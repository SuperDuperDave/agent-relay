"""Account-installed Multithread execution and enrollment boundary."""


def account_launcher(*, compatibility=False):
    """Name the account entry; selection is not proof of installation or trust."""
    from pathlib import Path
    import os
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin" / (
        "relay" if compatibility else "multithread")
