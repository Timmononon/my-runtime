# ruff: noqa
# Pinned upstream source; see SOURCE_MANIFEST.json and NOTICE.md.
def is_empty_execute_response(input_list: list):
    if len(input_list) == 0:
        return True
    if len(input_list) == 1 and len(input_list[0]) == 0:
        return True
    return False
