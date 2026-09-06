FROM apache/spark:3.5.3-java17-python3

ARG LIVY_VERSION=0.8.0-incubating
ARG LIVY_SHA512=467c977d4c72c67c2f7fa08134b9d0eeb5123b843cba67b5840620dba521e7f8cd532e7ab00b97b3e73cd737658f8937b0fea6e8179242a5f1a6ce29ba7b1d4f
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSLo /tmp/livy.zip "https://archive.apache.org/dist/incubator/livy/${LIVY_VERSION}/apache-livy-${LIVY_VERSION}_2.12-bin.zip" \
 && echo "${LIVY_SHA512}  /tmp/livy.zip" | sha512sum -c - \
 && unzip -q /tmp/livy.zip -d /opt \
 && mv "/opt/apache-livy-${LIVY_VERSION}_2.12-bin" /opt/livy \
 && rm /tmp/livy.zip \
 && mkdir -p /opt/livy/logs /opt/livy/run \
 && chown -R 185:0 /opt/livy
COPY livy.conf /opt/livy/conf/livy.conf
COPY spark-defaults.conf /opt/spark/conf/spark-defaults.conf
USER 185
EXPOSE 8998
ENTRYPOINT ["/opt/livy/bin/livy-server"]
