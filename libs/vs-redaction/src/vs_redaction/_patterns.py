"""Shared private grammar behind the public diagnostic redactors."""

COMPACT_SECRET_NAMES = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authtoken",
    "awsaccesskeyid",
    "awssecretaccesskey",
    "bearertoken",
    "clientkey",
    "clientsecret",
    "databasepassword",
    "dbpassword",
    "githubtoken",
    "idtoken",
    "openaiapikey",
    "privatekey",
    "refreshtoken",
    "secretkey",
    "sessiontoken",
)
COMPACT_SECRET_PATTERN = "|".join(COMPACT_SECRET_NAMES)
SECRET_NAME = (
    r"(?:(?i:(?:[a-z0-9]+[_-])+(?:token|password|secret|key)"
    r"(?:[_-][a-z0-9]+)*|" + COMPACT_SECRET_PATTERN + r"|password|secret|token)|"
    r"[A-Za-z0-9]*(?:Token|Password|Secret|Key)(?:[A-Z][A-Za-z0-9]*)?)"
)

__all__ = ["SECRET_NAME"]
