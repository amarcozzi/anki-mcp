FROM golang:1.27 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /anki-mcp ./cmd/anki-mcp

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /anki-mcp /anki-mcp
EXPOSE 8080
ENTRYPOINT ["/anki-mcp"]
