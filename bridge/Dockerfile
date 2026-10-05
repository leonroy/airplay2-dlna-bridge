FROM alpine:3.24
RUN apk add --no-cache python3 ffmpeg
COPY server.py /app/server.py
COPY web/ /app/web/
CMD ["python3", "-u", "/app/server.py"]
