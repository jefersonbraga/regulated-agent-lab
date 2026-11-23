# Railway model-link (see start.sh). Deploy: railway up infra/railway/model-link --path-as-root -s model-link
FROM tailscale/tailscale:v1.84.3
RUN apk add --no-cache socat
# Smaller tailnet MTU (default 1280): the route from the home network to Railway drops packets
# of ~1.3 KB or more without an ICMP error, so model answers over ~1 KB hung until the API's
# LLM timeout while short ones arrived. With 1200 the netstack here advertises a smaller TCP MSS
# and the model host sends segments that fit. Override with the TS_DEBUG_MTU variable.
ENV TS_DEBUG_MTU=1200
COPY --chmod=0755 start.sh /usr/local/bin/model-link
ENTRYPOINT ["/usr/local/bin/model-link"]
