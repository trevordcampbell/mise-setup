// The publisher's full desktop update is a Brotli-compressed tar file.
const { createReadStream, createWriteStream } = require('node:fs');
const { pipeline } = require('node:stream/promises');
const { createBrotliDecompress } = require('node:zlib');
async function* limitSize(source) {
  let bytes = 0;
  for await (const chunk of source) {
    bytes += chunk.length;
    if (bytes > 2 ** 31) throw new Error('Discord update exceeds the extraction limit');
    yield chunk;
  }
}
pipeline(createReadStream(process.argv[2]), createBrotliDecompress(), limitSize,
  createWriteStream(process.argv[3], { flags: 'wx' })).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
