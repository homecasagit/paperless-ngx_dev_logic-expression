import dataclasses
import datetime
import hashlib
import os
import uuid
from pathlib import Path
from subprocess import CompletedProcess
from subprocess import run
from typing import Dict
from typing import List
from typing import Optional
from typing import Type

import dateutil
import magic
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from filelock import FileLock
from rest_framework.reverse import reverse

from .classifier import load_classifier
from .file_handling import create_source_path_directory
from .file_handling import generate_unique_filename
from .loggers import LoggingMixin
from .models import Correspondent
from .models import Document
from .models import DocumentType
from .models import FileInfo
from .models import Tag
from .parsers import DocumentParser
from .parsers import get_parser_class_for_mime_type
from .parsers import parse_date
from .parsers import ParseError
from .signals import document_consumption_finished
from .signals import document_consumption_started

MESSAGE_DOCUMENT_ALREADY_EXISTS = "document_already_exists"
MESSAGE_FILE_NOT_FOUND = "file_not_found"
MESSAGE_PRE_CONSUME_SCRIPT_NOT_FOUND = "pre_consume_script_not_found"
MESSAGE_PRE_CONSUME_SCRIPT_ERROR = "pre_consume_script_error"
MESSAGE_POST_CONSUME_SCRIPT_NOT_FOUND = "post_consume_script_not_found"
MESSAGE_POST_CONSUME_SCRIPT_ERROR = "post_consume_script_error"
MESSAGE_NEW_FILE = "new_file"
MESSAGE_UNSUPPORTED_TYPE = "unsupported_type"
MESSAGE_PARSING_DOCUMENT = "parsing_document"
MESSAGE_GENERATING_THUMBNAIL = "generating_thumbnail"
MESSAGE_PARSE_DATE = "parse_date"
MESSAGE_SAVE_DOCUMENT = "save_document"
MESSAGE_FINISHED = "finished"


@dataclasses.dataclass
class DocumentOverrides:
    filename: Optional[str] = None
    title: Optional[str] = None
    correspondent_id: Optional[int] = None
    document_type_id: Optional[int] = None
    tag_ids: Optional[List[int]] = None
    created: Optional[datetime.datetime] = None

    def as_dict(self) -> Dict:
        return {
            "filename": self.filename,
            "title": self.title,
            "correspondent_id": self.correspondent_id,
            "document_type_id": self.document_type_id,
            "tag_ids": self.tag_ids,
            "created": self.created.isoformat() if self.created else None,
        }

    @classmethod
    def from_dict(data: Dict) -> "DocumentOverrides":
        return DocumentOverrides(
            data["filename"],
            data["title"],
            data["correspondent_id"],
            data["document_type_id"],
            data["tag_ids"],
            dateutil.parser.isoparse(data["created"]) if data["created"] else None,
        )


@dataclasses.dataclass
class ConsumeDocument:
    path: Path
    mime_type: str = dataclasses.field(init=False)
    overrides: DocumentOverrides = dataclasses.field(default_factory=DocumentOverrides)

    def __post_init__(self):
        # Always fully qualify the path first thing
        self.path = self.path.resolve()

        # Get the file type once at init
        self.mime_type = magic.from_file(self.path)

    @property
    def checksum(self) -> str:
        return hashlib.md5(self.path.read_bytes()).hexdigest()

    def as_dict(self) -> Dict:
        return {
            "path": str(self.path),
            "overrides": self.overrides.as_dict(),
        }

    @classmethod
    def from_dict(data: Dict) -> "ConsumeDocument":
        doc = ConsumeDocument(data["path"])
        doc.overrides = DocumentOverrides.from_dict(data["overrides"])
        return doc


class ConsumerError(Exception):
    pass


class Consumer(LoggingMixin):

    logging_name = "paperless.consumer"

    def _send_progress(
        self,
        current_progress,
        max_progress,
        status,
        message=None,
        document_id=None,
    ):
        payload = {
            "filename": self.file_in_work.path.name,
            "task_id": self.task_id,
            "current_progress": current_progress,
            "max_progress": max_progress,
            "status": status,
            "message": message,
            "document_id": document_id,
        }
        async_to_sync(self.channel_layer.group_send)(
            "status_updates",
            {"type": "status_update", "data": payload},
        )

    def _fail(
        self,
        message,
        log_message=None,
        exc_info=None,
        exception: Optional[Exception] = None,
    ):
        self._send_progress(100, 100, "FAILED", message)
        self.log("error", log_message or message, exc_info=exc_info)
        raise ConsumerError(
            f"{self.file_in_work.path.name}: {log_message or message}",
        ) from exception

    def __init__(self, file: ConsumeDocument):
        super().__init__()
        self.file_in_work = file
        self.task_id = str(uuid.uuid4())
        self.channel_layer = get_channel_layer()

    def pre_check_file_exists(self):
        if not self.file_in_work.path.exists():
            self._fail(
                MESSAGE_FILE_NOT_FOUND,
                f"Cannot consume {self.file_in_work.path}: File not found.",
            )

    def pre_check_duplicate(self):
        checksum = self.file_in_work.checksum
        existing_doc = Document.objects.filter(
            Q(checksum=checksum) | Q(archive_checksum=checksum),
        )
        if existing_doc.exists():
            if settings.CONSUMER_DELETE_DUPLICATES:
                os.unlink(self.file_in_work.path)
            self._fail(
                MESSAGE_DOCUMENT_ALREADY_EXISTS,
                f"Not consuming {self.file_in_work.path.name}: It is a duplicate of"
                f" {existing_doc.get().title} (#{existing_doc.get().pk})",
            )

    def pre_check_directories(self):
        os.makedirs(settings.SCRATCH_DIR, exist_ok=True)
        os.makedirs(settings.THUMBNAIL_DIR, exist_ok=True)
        os.makedirs(settings.ORIGINALS_DIR, exist_ok=True)
        os.makedirs(settings.ARCHIVE_DIR, exist_ok=True)

    def run_pre_consume_script(self):
        if not settings.PRE_CONSUME_SCRIPT:
            return

        if not os.path.isfile(settings.PRE_CONSUME_SCRIPT):
            self._fail(
                MESSAGE_PRE_CONSUME_SCRIPT_NOT_FOUND,
                f"Configured pre-consume script "
                f"{settings.PRE_CONSUME_SCRIPT} does not exist.",
            )

        self.log("info", f"Executing pre-consume script {settings.PRE_CONSUME_SCRIPT}")

        filepath_arg = str(self.file_in_work.path)

        script_env = os.environ.copy()
        script_env["DOCUMENT_SOURCE_PATH"] = filepath_arg

        try:
            completed_proc = run(
                args=[
                    settings.PRE_CONSUME_SCRIPT,
                    filepath_arg,
                ],
                env=script_env,
                capture_output=True,
            )

            self._log_script_outputs(completed_proc)

            # Raises exception on non-zero output
            completed_proc.check_returncode()

        except Exception as e:
            self._fail(
                MESSAGE_PRE_CONSUME_SCRIPT_ERROR,
                f"Error while executing pre-consume script: {e}",
                exc_info=True,
                exception=e,
            )

    def run_post_consume_script(self, document):
        if not settings.POST_CONSUME_SCRIPT:
            return

        if not os.path.isfile(settings.POST_CONSUME_SCRIPT):
            self._fail(
                MESSAGE_POST_CONSUME_SCRIPT_NOT_FOUND,
                f"Configured post-consume script "
                f"{settings.POST_CONSUME_SCRIPT} does not exist.",
            )

        self.log(
            "info",
            f"Executing post-consume script {settings.POST_CONSUME_SCRIPT}",
        )

        script_env = os.environ.copy()

        script_env["DOCUMENT_ID"] = str(document.pk)
        script_env["DOCUMENT_CREATED"] = str(document.created)
        script_env["DOCUMENT_MODIFIED"] = str(document.modified)
        script_env["DOCUMENT_ADDED"] = str(document.added)
        script_env["DOCUMENT_FILE_NAME"] = document.get_public_filename()
        script_env["DOCUMENT_SOURCE_PATH"] = os.path.normpath(document.source_path)
        script_env["DOCUMENT_ARCHIVE_PATH"] = os.path.normpath(
            str(document.archive_path),
        )
        script_env["DOCUMENT_THUMBNAIL_PATH"] = os.path.normpath(
            document.thumbnail_path,
        )
        script_env["DOCUMENT_DOWNLOAD_URL"] = reverse(
            "document-download",
            kwargs={"pk": document.pk},
        )
        script_env["DOCUMENT_THUMBNAIL_URL"] = reverse(
            "document-thumb",
            kwargs={"pk": document.pk},
        )
        script_env["DOCUMENT_CORRESPONDENT"] = str(document.correspondent)
        script_env["DOCUMENT_TAGS"] = str(
            ",".join(document.tags.all().values_list("name", flat=True)),
        )
        script_env["DOCUMENT_ORIGINAL_FILENAME"] = str(document.original_filename)

        try:
            completed_proc = run(
                args=[
                    settings.POST_CONSUME_SCRIPT,
                    str(document.pk),
                    document.get_public_filename(),
                    os.path.normpath(document.source_path),
                    os.path.normpath(document.thumbnail_path),
                    reverse("document-download", kwargs={"pk": document.pk}),
                    reverse("document-thumb", kwargs={"pk": document.pk}),
                    str(document.correspondent),
                    str(",".join(document.tags.all().values_list("name", flat=True))),
                ],
                env=script_env,
                capture_output=True,
            )

            self._log_script_outputs(completed_proc)

            # Raises exception on non-zero output
            completed_proc.check_returncode()

        except Exception as e:
            self._fail(
                MESSAGE_POST_CONSUME_SCRIPT_ERROR,
                f"Error while executing post-consume script: {e}",
                exc_info=True,
                exception=e,
            )

    def try_consume_file(
        self,
    ) -> Document:
        """
        Return the document object if it was successfully created.
        """
        self._send_progress(0, 100, "STARTING", MESSAGE_NEW_FILE)

        # this is for grouping logging entries for this particular file
        # together.

        self.renew_logging_group()

        # Make sure that preconditions for consuming the file are met.

        self.pre_check_file_exists()
        self.pre_check_directories()
        self.pre_check_duplicate()

        self.log("info", f"Consuming {self.file_in_work.path.name}")

        # Determine the parser class.
        self.log("debug", f"Detected mime type: {self.file_in_work.mime_type}")

        # Based on the mime type, get the parser for that type
        parser_class: Optional[Type[DocumentParser]] = get_parser_class_for_mime_type(
            self.file_in_work.mime_type,
        )
        if not parser_class:
            self._fail(
                MESSAGE_UNSUPPORTED_TYPE,
                f"Unsupported mime type {self.file_in_work.mime_type}",
            )

        # Notify all listeners that we're going to do some work.

        document_consumption_started.send(
            sender=self.__class__,
            filename=self.file_in_work.path,
            logging_group=self.logging_group,
        )

        self.run_pre_consume_script()

        def progress_callback(current_progress, max_progress):
            # recalculate progress to be within 20 and 80
            p = int((current_progress / max_progress) * 50 + 20)
            self._send_progress(p, 100, "WORKING")

        # This doesn't parse the document yet, but gives us a parser.

        document_parser: DocumentParser = parser_class(
            self.logging_group,
            progress_callback,
        )

        self.log("debug", f"Parser: {type(document_parser).__name__}")

        # However, this already created working directories which we have to
        # clean up.

        # Parse the document. This may take some time.

        text = None
        date = None
        thumbnail = None
        archive_path = None

        try:
            self._send_progress(20, 100, "WORKING", MESSAGE_PARSING_DOCUMENT)
            self.log("debug", f"Parsing {self.file_in_work.path.name}...")
            document_parser.parse(
                self.file_in_work.path,
                self.file_in_work.mime_type,
                self.file_in_work.path.name,
            )

            self.log(
                "debug",
                f"Generating thumbnail for {self.file_in_work.path.name}...",
            )
            self._send_progress(70, 100, "WORKING", MESSAGE_GENERATING_THUMBNAIL)
            thumbnail = document_parser.get_thumbnail(
                self.file_in_work.path,
                self.file_in_work.mime_type,
                self.file_in_work.path.name,
            )

            text = document_parser.get_text()
            date = document_parser.get_date()
            if date is None:
                self._send_progress(90, 100, "WORKING", MESSAGE_PARSE_DATE)
                date = parse_date(self.file_in_work.path.name, text)
            archive_path = document_parser.get_archive_path()

        except ParseError as e:
            document_parser.cleanup()
            self._fail(
                str(e),
                f"Error while consuming document {self.file_in_work.path.name}: {e}",
                exc_info=True,
                exception=e,
            )

        # Prepare the document classifier.

        # TODO: I don't really like to do this here, but this way we avoid
        #   reloading the classifier multiple times, since there are multiple
        #   post-consume hooks that all require the classifier.

        classifier = load_classifier()

        self._send_progress(95, 100, "WORKING", MESSAGE_SAVE_DOCUMENT)
        # now that everything is done, we can start to store the document
        # in the system. This will be a transaction and reasonably fast.
        try:
            with transaction.atomic():

                # store the document.
                document = self._store(
                    text=text,
                    date=date,
                    mime_type=self.file_in_work.mime_type,
                )

                # If we get here, it was successful. Proceed with post-consume
                # hooks. If they fail, nothing will get changed.

                document_consumption_finished.send(
                    sender=self.__class__,
                    document=document,
                    logging_group=self.logging_group,
                    classifier=classifier,
                )

                # After everything is in the database, copy the files into
                # place. If this fails, we'll also rollback the transaction.
                with FileLock(settings.MEDIA_LOCK):
                    document.filename = generate_unique_filename(document)
                    create_source_path_directory(document.source_path)

                    self._write(
                        document.storage_type,
                        self.file_in_work.path,
                        document.source_path,
                    )

                    self._write(
                        document.storage_type,
                        thumbnail,
                        document.thumbnail_path,
                    )

                    if archive_path and os.path.isfile(archive_path):
                        document.archive_filename = generate_unique_filename(
                            document,
                            archive_filename=True,
                        )
                        create_source_path_directory(document.archive_path)
                        self._write(
                            document.storage_type,
                            archive_path,
                            document.archive_path,
                        )

                        with open(archive_path, "rb") as f:
                            document.archive_checksum = hashlib.md5(
                                f.read(),
                            ).hexdigest()

                # Don't save with the lock active. Saving will cause the file
                # renaming logic to acquire the lock as well.
                # This triggers things like file renaming
                document.save()

                # Delete the file only if it was successfully consumed
                self.log("debug", f"Deleting file {self.file_in_work.path}")
                os.unlink(self.file_in_work.path)

                # https://github.com/jonaswinkler/paperless-ng/discussions/1037
                shadow_file = os.path.join(
                    os.path.dirname(self.file_in_work.path),
                    "._" + os.path.basename(self.file_in_work.path),
                )

                if os.path.isfile(shadow_file):
                    self.log("debug", f"Deleting file {shadow_file}")
                    os.unlink(shadow_file)

        except Exception as e:
            self._fail(
                str(e),
                f"The following error occurred while consuming "
                f"{self.file_in_work.path.name}: {e}",
                exc_info=True,
                exception=e,
            )
        finally:
            document_parser.cleanup()

        self.run_post_consume_script(document)

        self.log("info", f"Document {document} consumption finished")

        self._send_progress(100, 100, "SUCCESS", MESSAGE_FINISHED, document.id)

        # Return the most up to date fields
        document.refresh_from_db()

        return document

    def _store(
        self,
        text: str,
        date: Optional[datetime.datetime],
        mime_type: str,
    ) -> Document:

        # If someone gave us the original filename, use it instead of doc.

        file_info = FileInfo.from_filename(self.file_in_work.path.name)

        self.log("debug", "Saving record to database")

        if self.file_in_work.overrides.created is not None:
            create_date = self.file_in_work.overrides.created
            self.log(
                "debug",
                f"Creation date from post_documents parameter: {create_date}",
            )
        elif file_info.created is not None:
            create_date = file_info.created
            self.log("debug", f"Creation date from FileInfo: {create_date}")
        elif date is not None:
            create_date = date
            self.log("debug", f"Creation date from parse_date: {create_date}")
        else:
            stats = os.stat(self.file_in_work.path)
            create_date = timezone.make_aware(
                datetime.datetime.fromtimestamp(stats.st_mtime),
            )
            self.log("debug", f"Creation date from st_mtime: {create_date}")

        storage_type = Document.STORAGE_TYPE_UNENCRYPTED

        with open(self.file_in_work.path, "rb") as f:
            document = Document.objects.create(
                title=(self.file_in_work.overrides.title or file_info.title)[:127],
                content=text,
                mime_type=mime_type,
                checksum=hashlib.md5(f.read()).hexdigest(),
                created=create_date,
                modified=create_date,
                storage_type=storage_type,
                original_filename=self.file_in_work.path.name,
            )

        self.apply_overrides(document)

        document.save()

        return document

    def apply_overrides(self, document):
        if self.file_in_work.overrides.correspondent_id:
            document.correspondent = Correspondent.objects.get(
                pk=self.file_in_work.overrides.correspondent_id,
            )

        if self.file_in_work.overrides.document_type_id:
            document.document_type = DocumentType.objects.get(
                pk=self.file_in_work.overrides.document_type_id,
            )

        if self.file_in_work.overrides.tag_ids:
            for tag_id in self.file_in_work.overrides.tag_ids:
                document.tags.add(Tag.objects.get(pk=tag_id))

    def _write(self, storage_type, source, target):
        with open(source, "rb") as read_file:
            with open(target, "wb") as write_file:
                write_file.write(read_file.read())

    def _log_script_outputs(self, completed_process: CompletedProcess):
        """
        Decodes a process stdout and stderr streams and logs them to the main log
        """
        # Log what the script exited as
        self.log(
            "info",
            f"{completed_process.args[0]} exited {completed_process.returncode}",
        )

        # Decode the output (if any)
        if len(completed_process.stdout):
            stdout_str = (
                completed_process.stdout.decode("utf8", errors="ignore")
                .strip()
                .split(
                    "\n",
                )
            )
            self.log("info", "Script stdout:")
            for line in stdout_str:
                self.log("info", line)

        if len(completed_process.stderr):
            stderr_str = (
                completed_process.stderr.decode("utf8", errors="ignore")
                .strip()
                .split(
                    "\n",
                )
            )

            self.log("warning", "Script stderr:")
            for line in stderr_str:
                self.log("warning", line)
