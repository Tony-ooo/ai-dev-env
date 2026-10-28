"""websockify auth plugin that reads "user:pass" from a file.

Used instead of BasicHTTPAuth so the password never appears on the process
command line (a file path is not a secret).
"""

from websockify.auth_plugins import BasicHTTPAuth


class FileAuth(BasicHTTPAuth):
    def __init__(self, src=None):
        self.path = src

    def validate_creds(self, username, password):
        if not self.path:
            return False
        try:
            with open(self.path, encoding="utf-8") as handle:
                expected = handle.read().strip()
        except OSError:
            return False
        return f"{username}:{password}" == expected
