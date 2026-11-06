class NotesError(Exception):
    """notes の業務エラーの基底。"""


class ValidationError(NotesError):
    pass


class NotFound(NotesError):
    pass


class Forbidden(NotesError):
    pass


class Conflict(NotesError):
    pass


class RateLimited(NotesError):
    pass
