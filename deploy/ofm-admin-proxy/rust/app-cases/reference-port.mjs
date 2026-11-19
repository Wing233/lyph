// rust-anything allocates each replay server a port via PORT.
process.env.OFM_PROXY_PORT = process.env.PORT
await import('../../proxy.mjs')
