# The lab's SQL-injection scanner, built locally at `lab/run.py --setup` from a
# pinned, official source: sqlmap from PyPI (published by the sqlmap project,
# sqlmap.org) on the official Python image, pinned by digest. Nothing of
# sqlmap's GPLv2 code lives in this repository; this file only references it,
# the same way the ZAP scanner is referenced by digest. Built on the user's
# machine, never distributed.
#
# Moving to a newer base or sqlmap is a change to this file, reviewed like code.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ARG SQLMAP_VERSION
RUN pip install --no-cache-dir "sqlmap==${SQLMAP_VERSION}" \
    && useradd --system --uid 10001 --no-create-home redlab

USER redlab
# The compose service sets the full command (sqlmap -u ... --batch ...).
ENTRYPOINT []
