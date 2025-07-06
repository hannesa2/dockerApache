FROM httpd:2.4
RUN apt-get update && apt-get install -y curl lsb-release
RUN uname -a
RUN lsb_release -a

WORKDIR /src
COPY . .
