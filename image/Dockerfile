# alia-runtime: Debian 13 with the Alia Pad platform baseline, as on the handheld, plus the
# supervisor that runs a cart the way the handheld does (docs/design.md).
#   built by image/build.sh (context: the repository root)
FROM debian:trixie
ARG DEBIAN_FRONTEND=noninteractive
# Mesa comes from trixie-backports, as on the handheld: one install, so that everything
# pulling in Mesa (sway) takes the same version
RUN echo 'deb http://deb.debian.org/debian trixie-backports main' > /etc/apt/sources.list.d/backports.list \
 && printf 'Package: src:mesa\nPin: release n=trixie-backports\nPin-Priority: 990\n' > /etc/apt/preferences.d/mesa \
 && apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      libegl1 libegl-mesa0 libgles2 libgbm1 mesa-libgallium \
      libwayland-client0 libwayland-egl1 libsdl3-0 libasound2t64 pipewire-alsa \
      python3 fonts-noto-cjk \
      sway grim wtype python3-venv ca-certificates \
      pipewire pipewire-bin wireplumber iptables \
 && rm -rf /var/lib/apt/lists/*
# the supervisor's own Python packages (the overlay draws with Pillow): a separate
# environment, so carts can't come to depend on what the handheld doesn't have
RUN python3 -m venv /opt/alia-runtime/venv && /opt/alia-runtime/venv/bin/pip install -q pillow==11.3.0 websockets==15.0.1
# alia-sdk, where the handheld has python3-alia: in the system Python, for carts too
COPY image/wheels/ /tmp/wheels/
RUN /opt/alia-runtime/venv/bin/pip install -q --no-deps --target /usr/local/lib/python3.13/dist-packages /tmp/wheels/alia_sdk-*.whl \
 && rm -rf /tmp/wheels /usr/local/lib/python3.13/dist-packages/bin
COPY supervisor/alia_runtime /opt/alia-runtime/alia_runtime
COPY supervisor/web /opt/alia-runtime/web
# the browser mode's sound (its own PipeWire, a silent sink)
COPY image/pipewire/alia-browser.conf /etc/pipewire/pipewire.conf.d/alia-browser.conf
WORKDIR /opt/alia-runtime
ENTRYPOINT ["python3", "-m", "alia_runtime"]
