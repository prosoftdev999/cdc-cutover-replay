FROM python:3.13-slim-bookworm AS guard-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY init_guard.c /tmp/init_guard.c
COPY fs_guard.c /tmp/fs_guard.c
RUN gcc -O2 -Wall -Wextra -Werror -o /tmp/task-init /tmp/init_guard.c \
    && gcc -O2 -Wall -Wextra -Werror -fPIC -shared -o /tmp/task-fs-guard.so /tmp/fs_guard.c -ldl

FROM python:3.13-slim-bookworm

WORKDIR /app

COPY data/ /evidence/
COPY --from=guard-builder /tmp/task-init /usr/local/bin/task-init
COPY --from=guard-builder /tmp/task-fs-guard.so /usr/local/lib/task-fs-guard.so

RUN mkdir -p /home/agent /app/output /logs/agent /logs/verifier /logs/artifacts \
    && ln -s /evidence /app/data \
    && chown -R 10001:10001 /home/agent /app/output /logs/agent \
    && chown root:root /logs /logs/verifier /logs/artifacts /usr/local/bin/task-init /usr/local/lib/task-fs-guard.so \
    && chmod 0755 /logs /logs/agent \
    && chmod 0555 /logs/verifier /logs/artifacts \
    && chmod 4755 /usr/local/bin/task-init \
    && chmod 0555 /usr/local/lib/task-fs-guard.so

USER 10001:10001
ENV HOME=/home/agent
ENV LD_PRELOAD=/usr/local/lib/task-fs-guard.so

ENTRYPOINT ["/usr/local/bin/task-init"]
CMD ["sleep", "infinity"]
