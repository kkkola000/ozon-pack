class DomainError(Exception):
    """Expected error safe to show to the operator."""


class Forbidden(DomainError):
    pass


class Conflict(DomainError):
    pass


class NotFound(DomainError):
    pass


class RateLimited(DomainError):
    pass
