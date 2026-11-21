FROM golang:1.22.5-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY mech ./mech
COPY cmd ./cmd
RUN CGO_ENABLED=0 go build -trimpath -o /out/bot ./cmd/bot

FROM debian:bookworm-slim
COPY --from=build /out/bot /bot
USER 65534
ENTRYPOINT ["/bot"]
