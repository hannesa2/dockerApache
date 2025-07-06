https://www.docker.com/blog/multi-arch-images/

```
docker buildx ls
docker buildx create --name apacheGenalogie
docker buildx use apacheGenalogie
docker buildx inspect --bootstrap
cat <<EOF > Dockerfile\nFROM ubuntu\nRUN apt-get update && apt-get install -y curl\nWORKDIR /src\nCOPY . .\nEOF
docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t hannesa2/demo:latest --push .
```
