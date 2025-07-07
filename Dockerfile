# 22.04
FROM ubuntu:jammy

RUN apt-get update && apt-get install -y curl lsb-release
RUN uname -a
RUN lsb_release -a

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get install -y php mysql-server mysql-client wget
RUN apt-get install -y systemd-sysv ubuntu-standard
RUN apt-get install -y apache2 libapache2-mod-php
RUN systemctl enable apache2
RUN /etc/init.d/apache2 start
#RUN systemctl start apache2
#RUN systemctl status apache2

#RUN wget http://localhost
#RUN cat index.html

WORKDIR /src
COPY . .
