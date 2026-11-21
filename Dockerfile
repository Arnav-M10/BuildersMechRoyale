# mechfight/1 bot. Standard library only; the base image is pinned by digest so rebuilds are identical.
FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /bot
COPY bot.py geo.py ./
COPY arena ./arena
# Precompute the map model (nav grid, solids) from the kit's map copy; at runtime it is used
# only when its hash matches the mounted /arena files, otherwise rebuilt in about a second.
RUN python3 geo.py /bot/arena /bot/cache.pkl \
 && python3 -m compileall -q /bot \
 && chmod -R a+rX /bot
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
