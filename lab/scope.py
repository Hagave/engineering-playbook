"""What the lab may reach: a path on the target service, never another host,
and a file of this computer only when it sits inside the project."""
import os
import re

PATH_RE = re.compile(r'^/(?!/)[A-Za-z0-9._~!$&\'()*+,;=:%/-]*$')


def within(path: str, root: str) -> bool:
    """Whether `path`, links resolved, is `root` or sits under it. A mount of
    `/work/shop-data` is not inside `/work/shop`: the separator guards that."""
    if not path or not root:
        return False
    try:
        real_root, real = os.path.realpath(root), os.path.realpath(path)
    except (OSError, ValueError):
        return False
    return real == real_root or real.startswith(real_root + os.sep)
