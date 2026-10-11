import { describe, expect, it } from 'vitest';

import { fileToBase64 } from '@/api/operations';

describe('fileToBase64', () => {
  it('encodes a file larger than one chunk byte for byte', async () => {
    const bytes = Uint8Array.from({ length: 0x8000 * 2 + 7 }, (_, index) => index % 256);

    const encoded = await fileToBase64(new Blob([bytes]));

    expect(Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0))).toEqual(bytes);
  });

  it('encodes an empty file as nothing', async () => {
    expect(await fileToBase64(new Blob([]))).toBe('');
  });
});
