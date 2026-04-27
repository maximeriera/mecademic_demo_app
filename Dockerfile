FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update
RUN apt-get upgrade -y

# 2. Now run your normal update and install other tools safely
RUN apt-get update && apt-get install -y \
    nginx \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /webapp
COPY . /webapp/

RUN cp index.html /var/www/html/index.html

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies using the venv's pip
RUN pip install --no-cache-dir --upgrade pip && \
    if [ -f requirements_unix.txt ]; then pip install --no-cache-dir -r requirements_unix.txt; fi

# --- NGINX CONFIGURATION ---
RUN echo ' \
server { \
    listen 80 default_server; \
    server_name _; \
    root /var/www/html; \
    index index.html; \
    location / { \
        try_files $uri $uri/ /index.html; \
    } \
} \
server { \
    listen 80; \
    server_name mecademo.link meca.demo; \
    root /var/www/html; \
    index index.html; \
    location = /app { \
        proxy_pass http://127.0.0.1:5000/; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; \
    } \
    location /app/ { \
        rewrite ^/app/?(.*)$ /$1 break; \
        proxy_pass http://127.0.0.1:5000; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; \
    } \
    location /api/ { \
        proxy_pass http://127.0.0.1:5000; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; \
    } \
    location /static/ { \
        proxy_pass http://127.0.0.1:5000; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; \
    } \
    location / { \
        try_files $uri $uri/ /index.html; \
    } \
}' > /etc/nginx/sites-available/default

# Ensure Nginx can reach the socket/app if needed
RUN chown -R www-data:www-data /webapp
# Expose port 80 (Nginx)
EXPOSE 80

# Start the Flask app and force it to listen on all network interfaces
CMD python3 app.py --host=127.0.0.1 --port=5000 & sleep 5 && nginx -g "daemon off;"