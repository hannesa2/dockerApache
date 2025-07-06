FROM ubuntu
RUN apt-get update && apt-get install -y curl lsb-release
RUN uname -a
RUN lsb_release -a

RUN DEBIAN_FRONTEND=noninteractive TZ=Etc/UTC apt-get install -y php mysql-server mysql-client

WORKDIR /src
COPY . .
