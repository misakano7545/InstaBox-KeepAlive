FROM python:3.12-slim
WORKDIR /app
COPY instabox_keepalive.py server.py ./
ENV PORT=8080
CMD ["python3", "server.py"]
