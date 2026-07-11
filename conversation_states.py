# conversation_states.py
# ---------------  /add  ---------------
# States for the add-course flow. Keep existing values stable and
# introduce ADD_PARENT and ADD_COACH so the flow can ask for a parent
# category, then a coach, then the course name and link.
ADD_NAME, ADD_LINK, ADD_CATEGORY, ADD_PARENT, ADD_COACH = range(5)
# ---------------  /start  ---------------
START_AWAIT_NAME = 7
# ---------------  /create_category  ---------------
CREATE_CAT_NAME = 10
CREATE_CAT_PARENT = 11
# ---------------  /delete_all  ---------------
DELETE_ALL, CONFIRM_DELETE, CANCEL_DELETE = range(20, 23)
# ---------------  search  ---------------
SEARCH_QUERY = 30
MAX_CATEGORY_NAME_LENGTH = 30
