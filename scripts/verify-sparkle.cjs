// Sparkle signs the complete archive with Ed25519. Node is part of the global Mise manifest.
const { createPublicKey, verify } = require('node:crypto');
const { readFileSync } = require('node:fs');
const [publicKey, signature, archive] = process.argv.slice(2);
const key = createPublicKey({
  key: { kty: 'OKP', crv: 'Ed25519', x: Buffer.from(publicKey, 'base64').toString('base64url') },
  format: 'jwk',
});
if (!verify(null, readFileSync(archive), key, Buffer.from(signature, 'base64'))) {
  console.error('Sparkle archive signature does not match the installed publisher key.');
  process.exitCode = 1;
}
