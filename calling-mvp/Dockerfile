FROM node:24-bookworm-slim AS dial_tools
RUN npm install --prefix /opt/dial --no-audit --no-fund @getdial/cli@0.56.0
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DIAL_NO_AUTO_UPDATE=1 \
    XDG_DATA_HOME=/workspace/calling-mvp/.local/share PATH=/app/.venv/bin:/opt/dial/node_modules/.bin:$PATH
COPY --from=dial_tools /usr/local/bin/node /usr/local/bin/node
COPY --from=dial_tools /opt/dial /opt/dial
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY data ./data
RUN pip install --no-cache-dir --disable-pip-version-check uv && uv sync --frozen --no-dev
EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "1"]
