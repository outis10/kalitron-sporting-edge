FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY migrations/ migrations/

RUN pip install --no-cache-dir -e ".[dev]"

CMD ["uvicorn", "sporting_edge.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
