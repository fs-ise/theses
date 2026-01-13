FROM ghcr.io/quarto-dev/quarto:latest

# python stuff from before …
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git \
        texlive-full \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir jupyter ipykernel matplotlib folium pandas plotly
RUN python3 -m ipykernel install --name=python3 --display-name "Python 3"

WORKDIR /project
