# An always-on Workstead relay node: relayd plus the announcer, nothing else.
# Only the relay modules go in (no git, no gh, no shell tools beyond busybox).
FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/workstead
COPY tools/ws/workstead/__init__.py tools/ws/workstead/relayd.py tools/ws/workstead/relay.py \
     tools/ws/workstead/nostr.py tools/ws/workstead/schnorr.py tools/ws/workstead/dag.py \
     tools/ws/workstead/logs.py tools/ws/workstead/config.py tools/ws/workstead/announce.py \
     tools/ws/workstead/gh.py tools/ws/workstead/findings.py tools/ws/workstead/toolbox.py tools/ws/workstead/guest.py \
     /opt/workstead/workstead/
RUN mkdir -p /data /keys && chown 65534:65534 /data /keys && chmod 0700 /keys
USER 65534:65534
EXPOSE 8080
CMD ["python", "-m", "workstead.relayd", "--host", "0.0.0.0", "--port", "8080", "--state", "/data"]
