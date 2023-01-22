import logging
import re

logger = logging.getLogger("paperless.match_complex")

# A lot to do here, code needs to be cleaned!


def query_bracket_checker(query: str) -> bool:
    # This function checks the useage of brackets.

    if (query.count("(") == 0) and (query.count(")") == 0):
        return True

    if query.count("(") != query.count(")"):
        return False

    # at every position in query: count of "("" must be
    # equal to or greater than the count of ")""
    regex_brackets = r"[\)]"
    closing_brackets = re.finditer(
        regex_brackets,
        query,
    )  # matches all closing brackets
    for item in closing_brackets:
        if query.count(")", 0, item.end()) > query.count("(", 0, item.end()):
            return False
            # once ANY closing bracket is found that has not been opended,
            # the checker exits and returns false

    return True


def syntax_checker(query: str) -> bool:
    # check brackets first:
    if query_bracket_checker(query) is False:
        logger.debug(
            "brackets are not correct",
        )
        return False

    # Double "&&" or "||" are solved directly now.
    # Other checks may be placed here.
    return True


def remove_unwanted_brackets(string: str) -> str:
    # This cleans any brackets that are directly closed again.
    # The query could not be evaluated if it would contain them.
    while string.find("()") >= 0:
        string = string.replace("()", "")
    return string


def regex_query_replace(regex: str, querystring: str, replstring: str) -> str:
    matches = re.finditer(regex, querystring)
    position_correction = (
        0  # we have to correct the position of the matches after each item
    )
    for item in matches:
        # remove spaces from querystring at corrected position
        querystring = (
            querystring[: item.start() + position_correction]
            + replstring
            + querystring[item.end() + position_correction :]
        )
        # adjust the correction factor (add the quantity of removed spaces to it)
        position_correction = (
            position_correction - (item.end() - item.start()) + len(replstring)
        )
    return querystring


def logic_match(sourcetext: str, querystring: str) -> bool:
    # This funktion takes a logical expression as querystring.
    # The query could be build using the following operators: [()|&!]
    # all other charactes will be parced as simple text and are matched
    # against the given sourcetext.

    # will be used to analyse the given querystring:
    regex_analyze = r"[^()|&!]+"
    # cleans any spaces that are not part of the string:
    regex_clean_spaces = r"(?<=[()|&!]) +(?=[()|&!])"
    # used to connect with AND: "!" and "(", it will also remove spaces,
    # so "text1 !text2" converts to "text1 and not text2":
    regex_special_connections = r"(?<=[^(|&! ]) *(?=[!\(])"
    # will be connected with AND, also removes spaces:
    regex_text_after_closingBracket = r"(?<=[\)]) *(?=[^())|&! ])"
    # find two or more "|", replace with single and remove spaces:
    regex_OR = r" *\|+ *"
    # find two or more "&", replace with single and remove spaces:
    regex_AND = r" *&+ *"

    # clean the query string so it does not fail
    # could be realized in a seperate function

    # Note:
    # To allow a search for spaces only, without any other characters
    # (= actually having " " as search-string), the syntax has to be
    # checked before the spaces_cleanup and spaces between two "&" must
    # then be preserved.
    # It is currently expected, that checks for " " would not be needed.

    logger.debug("Query before cleanup: " + querystring)

    # clean spaces:
    querystring = regex_query_replace(regex_clean_spaces, querystring, "")
    # use the special connections:
    querystring = regex_query_replace(regex_special_connections, querystring, "&")
    # connect closing brackets that are directly followed by text:
    querystring = regex_query_replace(regex_text_after_closingBracket, querystring, "&")
    # one or more "|", also remove spaces
    querystring = regex_query_replace(regex_OR, querystring, "|")
    # one or more "&", also remove spaces
    querystring = regex_query_replace(regex_AND, querystring, "&")
    # remove brackets that are opened and closed again immediately
    querystring = remove_unwanted_brackets(querystring)

    logger.debug("Query after cleanup: " + querystring)

    # Check Syntax
    if syntax_checker(querystring) is False:
        logger.debug("Syntax Error, can't process")
        return False

    # Analyse
    # This section extracts all plain thext sequences in the query
    # and matches it against the text (the document).
    matches = re.finditer(
        regex_analyze,
        querystring,
    )  # matches all plain text sequences in the advanced query
    corr = 0  # reuse the correction factor

    for match in matches:  # loop through each single plain text sequence
        # checks, weather the source contains
        # the currenty analysed plain text sequence
        found = bool(sourcetext.find(match.group(0)) + 1)

        # Replaces the analysed text with the result
        # Note: string.replace() would not work here:
        # If there was "bus" and "busstop" in the query,
        # bus in busstop would be replaced and busstop could
        # then not be replaced with the result
        querystring = (
            querystring[: match.start() + corr]
            + str(found)
            + querystring[match.end() + corr :]
        )
        # adjust correction value:
        corr = corr - (match.end() - match.start()) + len(str(found))

    # replaces the used operators with the python operators:
    querystring = (
        querystring.replace("|", " or ").replace("&", " and ").replace("!", " not ")
    )

    logger.debug(f"Query as logical expression: {querystring}")

    # check the querystring before using eval() on it
    replace_to_check = {"False", "True", "not", "and", "or", " ", "(", ")"}

    querycheck = querystring
    for item in replace_to_check:
        querycheck = querycheck.replace(item, "")

    if len(querycheck) > 0:
        logger.debug(
            "Something went wrong. .eval() is unsafe to use, "
            "will skip evaluation and return False",
        )
        result = False
    else:
        # evaluates the query if it is safe to use
        result = eval(querystring)

    logger.debug(f"Result: {result}")
    return result
