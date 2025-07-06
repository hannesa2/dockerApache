# 22.04
FROM ubuntu:jammy

RUN apt-get update && apt-get install -y curl lsb-release
RUN uname -a
RUN lsb_release -a

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get install -y php mysql-server mysql-client

WORKDIR /src
COPY . .
