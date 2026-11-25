# Postgres + pgBackRest, to restore from the nas1 repository on nas1 (or any Docker host).
FROM postgres:18.7-trixie
RUN apt-get update && apt-get install -y --no-install-recommends pgbackrest && rm -rf /var/lib/apt/lists/*
