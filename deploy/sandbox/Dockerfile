FROM python:3.11.13-slim-bookworm

RUN pip install --no-cache-dir numpy==2.4.3 pandas==3.0.1 pyarrow==23.0.1 scipy==1.17.1 scikit-learn==1.8.0 statsmodels==0.14.6 matplotlib==3.10.8
COPY run_job.py /opt/meridian/run_job.py
USER 65534:65534
WORKDIR /output
