FROM maven:3.8.8-eclipse-temurin-8 AS builder

WORKDIR /workspace
COPY . .
RUN mvn -B -pl ts-config-service,ts-station-service,ts-train-service,ts-travel-service,ts-route-service,ts-price-service \
        -am clean package -Dmaven.test.skip=true

ARG MODULE
RUN mkdir /output \
    && find "${MODULE}/target" -maxdepth 1 -type f -name '*.jar' \
        ! -name '*-sources.jar' ! -name '*-javadoc.jar' \
        -exec cp '{}' /output/app.jar \; \
    && test -s /output/app.jar

FROM eclipse-temurin:8-jre

ENV TZ=Asia/Shanghai
WORKDIR /app
COPY --from=builder /output/app.jar /app/app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-Xmx200m", "-jar", "/app/app.jar"]
