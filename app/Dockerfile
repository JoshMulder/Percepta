FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY backend backend

# Run as an unprivileged user (uid 10001) rather than root. This process
# terminates data from ground stations, which are physically unattended and
# outside our control - the one place in the system where untrusted input
# arrives - so it should never hold root inside the container.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
