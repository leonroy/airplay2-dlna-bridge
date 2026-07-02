FROM alpine:3.20
RUN apk add --no-cache python3 ffmpeg
COPY server.py /app/server.py
CMD ["python3", "-u", "/app/server.py"]
