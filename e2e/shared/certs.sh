#!/bin/sh
# Writes a throwaway test CA, and a certificate and PKCS12 keystore for each
# test host, into the directory given (default /certs). Run by the `certs`
# one-shot service; nothing here is a secret worth keeping.
#
#   ca.crt, ca-certificates.crt   the CA, the second named as a system bundle
#   <host>.crt, <host>.key        a server certificate for <host>, signed by it
#   <host>.p12                    the same, for WireMock (password: password)
set -eu

out=${1:-/certs}
cd "$out"

openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=rail e2e test CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" \
  -keyout ca.key -out ca.crt 2>/dev/null

for host in upstream.test evil.test; do
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$host" \
    -keyout "$host.key" -out "$host.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$host" > "$host.ext"
  openssl x509 -req -in "$host.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days 2 -extfile "$host.ext" -out "$host.crt" 2>/dev/null
  openssl pkcs12 -export -in "$host.crt" -inkey "$host.key" -name "$host" \
    -out "$host.p12" -passout pass:password
  rm "$host.csr" "$host.ext"
done

cp ca.crt ca-certificates.crt
# Read by WireMock's and Envoy's own users.
chmod a+r ./*
echo "certs: written to $out"
