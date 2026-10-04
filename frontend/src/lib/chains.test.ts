import { describe, expect, it } from 'vitest';

import {
  addressHint,
  CHAIN_KEYS,
  chainDisplayName,
  CHAINS,
  isPrivateExtendedKey,
  PRIVATE_KEY_RUN,
  PRIVATE_KEY_WARNING,
} from '@/lib/chains';
import { ADDRESSES, EXTENDED_KEYS, privateKeyShapedRun } from '@/test/fixtures';

/**
 * The hint table from the spec, one `describe` per row:
 *
 * | Typed | Hint |
 * |---|---|
 * | empty | the chain's format hint |
 * | a private extended key prefix, any chain | a private key: blocks submission |
 * | a public extended key prefix on Bitcoin | every address will be scanned |
 * | a public extended key prefix on Kaspa | only single addresses are supported |
 * | a Kaspa prefix while Bitcoin is selected | looks like Kaspa, with a switch |
 * | a Bitcoin shape while Kaspa is selected | looks like Bitcoin, with a switch |
 * | Kaspa selected, no `:` | Kaspa addresses include their network prefix |
 *
 * Mainnet shapes appear here only as prefixes far too short to be an address
 * (`bc1q`, `1A`, `kaspa:qq`, `xpub6`). The gitleaks rules match full-length
 * mainnet addresses, and none of these is one. Extended keys appear the same
 * way: a prefix and four characters, never the shape of a key (spec 031, R11).
 * A private key in particular is never written here at any length beyond that.
 */

describe('chain registry', () => {
  it('lists both chains the backend knows, in a stable order', () => {
    expect(CHAIN_KEYS).toEqual(['bitcoin', 'kaspa']);
  });

  it('names each chain for a person', () => {
    expect(chainDisplayName('bitcoin')).toBe('Bitcoin');
    expect(chainDisplayName('kaspa')).toBe('Kaspa');
  });

  it('falls back to the raw key for a chain this build does not know', () => {
    // `chain_key` is typed `string` on the wire. A chain added on the backend
    // before this build ships must still render as something.
    expect(chainDisplayName('litecoin')).toBe('litecoin');
    expect(chainDisplayName('')).toBe('');
  });

  it('does not mistake an inherited property for a chain', () => {
    // A lookup written with `in` or a bare index would find these on the
    // prototype and render "undefined" or a function's source.
    expect(chainDisplayName('toString')).toBe('toString');
    expect(chainDisplayName('constructor')).toBe('constructor');
    expect(chainDisplayName('__proto__')).toBe('__proto__');
  });
});

describe('addressHint: empty', () => {
  it.each(CHAIN_KEYS)('shows the %s format hint when nothing is typed', (chainKey) => {
    expect(addressHint(chainKey, '')).toEqual({ message: CHAINS[chainKey].formatHint });
  });

  it.each(CHAIN_KEYS)('treats whitespace alone as empty on %s', (chainKey) => {
    expect(addressHint(chainKey, '   \t')).toEqual({ message: CHAINS[chainKey].formatHint });
  });

  it('describes the Bitcoin prefixes, testnet included', () => {
    const hint = CHAINS.bitcoin.formatHint;

    for (const prefix of ['bc1', '1', '3', 'tb1', 'm', 'n', '2']) {
      expect(hint).toContain(prefix);
    }
    expect(hint).toMatch(/testnet/i);
  });

  it('describes the Kaspa prefixes and says the prefix is part of the address', () => {
    const hint = CHAINS.kaspa.formatHint;

    expect(hint).toContain('kaspa:');
    expect(hint).toContain('kaspatest:');
    expect(hint).toMatch(/prefix is part of the address/i);
  });
});

/** A prefix and a short tail: recognisably a key prefix, never key-shaped. */
function short(prefix: string): string {
  return `${prefix}8Zgx`;
}

const PUBLIC_PREFIXES = ['xpub', 'ypub', 'zpub', 'tpub', 'upub', 'vpub'] as const;
const MULTISIG_PREFIXES = ['Ypub', 'Zpub', 'Upub', 'Vpub'] as const;
const PRIVATE_PREFIXES = [
  'xprv',
  'yprv',
  'zprv',
  'tprv',
  'uprv',
  'vprv',
  'Yprv',
  'Zprv',
  'Uprv',
  'Vprv',
] as const;

const PRIVATE_KEY_MESSAGE =
  'This is a private key. Never enter a private key here or anywhere else. Enter the extended public key instead.';

describe('addressHint: a public extended key on Bitcoin', () => {
  it.each(PUBLIC_PREFIXES)('says every address of a %s will be scanned', (prefix) => {
    const hint = addressHint('bitcoin', short(prefix));

    expect(hint?.message).toMatch(/extended public key/i);
    expect(hint?.message).toMatch(/every address of this wallet will be scanned/i);
    expect(hint?.message).toMatch(/first scan takes about a minute/i);
    // Spec 031 accepts it on Bitcoin: nothing to switch to, nothing to block.
    expect(hint?.switchTo).toBeUndefined();
    expect(hint?.blocksSubmission).toBeUndefined();
  });

  it('recognises the prefix before anything else is typed', () => {
    expect(addressHint('bitcoin', 'tpub')?.message).toMatch(/will be scanned/i);
  });

  it('ignores surrounding whitespace', () => {
    expect(addressHint('bitcoin', '  vpub5Y')?.message).toMatch(/will be scanned/i);
    expect(addressHint('bitcoin', `zpub8Zgx\n`)?.message).toMatch(/will be scanned/i);
  });

  it.each([...MULTISIG_PREFIXES, 'XPUB', 'Xpub', 'TPUB', 'Tpub', 'VPUB'])(
    'does not promise a scan for %s, which is not a single-signature prefix as typed',
    (prefix) => {
      // Base58 is case-sensitive: `Ypub` is multisig and the server refuses it by name,
      // and `XPUB` is no prefix at all. Neither is blocked here either.
      const hint = addressHint('bitcoin', short(prefix));

      expect(hint?.message ?? '').not.toMatch(/will be scanned/i);
      expect(hint?.blocksSubmission).toBeUndefined();
    },
  );
});

describe('addressHint: a public extended key on Kaspa', () => {
  it.each([...PUBLIC_PREFIXES, ...MULTISIG_PREFIXES])(
    'says %s is not a single address on kaspa',
    (prefix) => {
      const hint = addressHint('kaspa', short(prefix));

      expect(hint?.message).toMatch(/only single addresses are supported/i);
      // No Kaspa wallet can take an extended key, and calling it Bitcoin would be a guess.
      expect(hint?.switchTo).toBeUndefined();
      expect(hint?.blocksSubmission).toBeUndefined();
    },
  );

  it('recognises the prefix in any case, since Kaspa has no extended key to confuse', () => {
    expect(addressHint('kaspa', 'XPUB8Zgx')?.message).toMatch(/only single addresses/i);
  });

  it('is checked before the Kaspa-looks-like-Bitcoin rule', () => {
    // A `tpub` on Kaspa is an extended key first; calling it "Bitcoin" and
    // offering a switch would lead straight into a second refusal.
    expect(addressHint('kaspa', 'tpubD6NzVbkrYhZ4')?.switchTo).toBeUndefined();
  });
});

describe('addressHint: a private extended key', () => {
  it.each(PRIVATE_PREFIXES.flatMap((prefix) => CHAIN_KEYS.map((chainKey) => [prefix, chainKey])))(
    'blocks %s on %s, before anything is sent',
    (prefix, chainKey) => {
      const hint = addressHint(chainKey as 'bitcoin' | 'kaspa', short(prefix));

      expect(hint?.message).toBe(PRIVATE_KEY_MESSAGE);
      expect(hint?.blocksSubmission).toBe(true);
      expect(hint?.switchTo).toBeUndefined();
    },
  );

  it.each(['tprv', 'XPRV', 'Tprv', '  zprv', `vprv\t`, 'TPRV8Zgx'])(
    'recognises %j whatever its case and padding',
    (typed) => {
      expect(addressHint('bitcoin', typed)?.blocksSubmission).toBe(true);
      expect(addressHint('kaspa', typed)?.blocksSubmission).toBe(true);
    },
  );

  it('is checked before the Kaspa-looks-like-Bitcoin rule', () => {
    expect(addressHint('kaspa', 'tprv8Zgx')?.switchTo).toBeUndefined();
  });

  it('never blocks an address', () => {
    for (const address of Object.values(ADDRESSES)) {
      for (const chainKey of CHAIN_KEYS) {
        expect(addressHint(chainKey, address)?.blocksSubmission).toBeUndefined();
      }
    }
  });

  it('never blocks a public key', () => {
    for (const prefix of [...PUBLIC_PREFIXES, ...MULTISIG_PREFIXES]) {
      for (const chainKey of CHAIN_KEYS) {
        expect(addressHint(chainKey, short(prefix))?.blocksSubmission).toBeUndefined();
      }
    }
  });
});

describe('isPrivateExtendedKey (spec 031, R2b)', () => {
  /** Unicode format characters (`Cf`), built from code points so the source shows them. */
  const FORMAT_CHARACTERS = [0x200b, 0x2060, 0x200e, 0xfeff, 0xad].map((point) =>
    String.fromCodePoint(point),
  );
  const ZERO_WIDTH_SPACE = String.fromCodePoint(0x200b);

  /** The pattern with its length floor lowered to one, so prefixes are proven on short text. */
  const SHORT_RUN = new RegExp(PRIVATE_KEY_RUN.source.replace('{100,}', '{1,}'));

  it('is exactly the ruling: a private prefix not continuing Base58, then 100 or more', () => {
    const base58 = '1-9A-HJ-NP-Za-km-z';
    expect(PRIVATE_KEY_RUN.source).toBe(`(?<![${base58}])(?:[xyztuv]|[YZUV])prv[${base58}]{100,}`);
    // No `g`: a global regex keeps `lastIndex` between calls and answers every other one
    // wrongly. No `i`: `XPRV` is no version byte (the prefix test handles case itself).
    expect(PRIVATE_KEY_RUN.flags).toBe('');
    expect(SHORT_RUN.source).not.toBe(PRIVATE_KEY_RUN.source);
  });

  it.each(['x', 'y', 'z', 't', 'u', 'v', 'Y', 'Z', 'U', 'V'])(
    'the run pattern knows the %sprv prefix, mainnet included, but not glued to Base58',
    (letter) => {
      expect(SHORT_RUN.test(`${letter}prva`)).toBe(true);
      expect(SHORT_RUN.test(` ${letter}prva`)).toBe(true);
      expect(SHORT_RUN.test(`"${letter}prva`)).toBe(true);
      expect(SHORT_RUN.test(`a${letter}prva`)).toBe(false);
      expect(SHORT_RUN.test(`5${letter}prva`)).toBe(false);
    },
  );

  it.each(['X', 'T', 'w', 'q', 'a', 'W'])('the run pattern does not know %sprv', (letter) => {
    expect(SHORT_RUN.test(`${letter}prva`)).toBe(false);
  });

  it('the run pattern is about private keys, not public ones', () => {
    for (const prefix of [...PUBLIC_PREFIXES, ...MULTISIG_PREFIXES]) {
      expect(SHORT_RUN.test(`${prefix}a`)).toBe(false);
    }
  });

  it('the run is Base58: 0, O, I and l end it', () => {
    for (const outside of ['0', 'O', 'I', 'l']) {
      expect(SHORT_RUN.test(`tprv${outside}`)).toBe(false);
    }
  });

  it.each(['tprv', 'uprv', 'vprv'] as const)(
    'a %s run of 100 is a private key anywhere in the value; of 99, nowhere',
    (prefix) => {
      const run = privateKeyShapedRun(prefix);
      const shortRun = privateKeyShapedRun(prefix, 99);
      expect(run).toHaveLength(104);

      for (const around of [
        (value: string) => value,
        (value: string) => `"${value}"`,
        (value: string) => `my key: ${value}`,
        (value: string) => `${value} is the one`,
      ]) {
        expect(isPrivateExtendedKey(around(run))).toBe(true);
        // A short run at the start is still a private prefix (rule a); behind text it is not.
        expect(isPrivateExtendedKey(`my key: ${shortRun}`)).toBe(false);
        expect(isPrivateExtendedKey(`"${shortRun}"`)).toBe(false);
      }
    },
  );

  it('a format character inside the body does not split the run', () => {
    const run = privateKeyShapedRun('vprv', 100);
    const split = `${run.slice(0, 54)}${ZERO_WIDTH_SPACE}${run.slice(54)}`;

    // Without removing it, neither half is 100 long.
    expect(PRIVATE_KEY_RUN.test(split)).toBe(false);
    expect(isPrivateExtendedKey(`my key: ${split}`)).toBe(true);
    // And a split 99-character run stays below the floor.
    const shortSplit = privateKeyShapedRun('vprv', 99);
    expect(
      isPrivateExtendedKey(
        `my key: ${shortSplit.slice(0, 50)}${ZERO_WIDTH_SPACE}${shortSplit.slice(50)}`,
      ),
    ).toBe(false);
  });

  it.each(
    FORMAT_CHARACTERS.map((character) => [character.codePointAt(0)?.toString(16), character]),
  )('a private prefix behind U+%s is still at the start', (_point, character) => {
    expect(isPrivateExtendedKey(`${character}xprv8Zgx`)).toBe(true);
    expect(isPrivateExtendedKey(` \t${character}Yprv8Zgx`)).toBe(true);
    expect(isPrivateExtendedKey(`${character}${character}tprv8Zgx`)).toBe(true);
    for (const chainKey of CHAIN_KEYS) {
      expect(addressHint(chainKey, `${character}zprv8Zgx`)?.blocksSubmission).toBe(true);
    }
  });

  it.each([
    ['at the start of the value', (run: string) => run],
    ['after a space', (run: string) => `my key ${run}`],
    ['after a quote', (run: string) => `"${run}`],
    ['after a character outside Base58', (run: string) => `0${run}`],
    ['after a stripped format character', (run: string) => `${ZERO_WIDTH_SPACE}${run}`],
    [
      'after a separator and a stripped format character',
      (run: string) => `my key:${ZERO_WIDTH_SPACE}${run}`,
    ],
  ] as const)('a run %s is a private key (the left boundary)', (_where, wrap) => {
    const run = privateKeyShapedRun('uprv');

    expect(isPrivateExtendedKey(wrap(run))).toBe(true);
    for (const chainKey of CHAIN_KEYS) {
      expect(addressHint(chainKey, wrap(run))?.blocksSubmission).toBe(true);
    }
  });

  it('a run glued onto Base58 text is accepted, by design (spec 031, R2b)', () => {
    // The one case the boundary gives up, so that a public key is never read as a private one.
    const run = privateKeyShapedRun('tprv');

    expect(isPrivateExtendedKey(`abc${run}`)).toBe(false);
    expect(isPrivateExtendedKey(`my key:abc${run}`)).toBe(false);
    // A format character between them is removed first, so it glues them the same way.
    expect(isPrivateExtendedKey(`xaaaaa${ZERO_WIDTH_SPACE}${run}`)).toBe(false);
  });

  it('a public-shaped key whose body carries a private-prefix run is not refused', () => {
    // The false positive the boundary removes: `uprv` and 102 Base58 characters inside a
    // vpub-shaped value of a key's length. Built in memory, test-network prefixes only.
    const publicShaped = `vpub5${privateKeyShapedRun('uprv', 102)}`;
    expect(publicShaped).toHaveLength(111);

    expect(isPrivateExtendedKey(publicShaped)).toBe(false);
    for (const chainKey of CHAIN_KEYS) {
      expect(addressHint(chainKey, publicShaped)?.blocksSubmission).toBeUndefined();
    }
    expect(addressHint('bitcoin', publicShaped)?.message).toBe(
      addressHint('bitcoin', EXTENDED_KEYS.vpub)?.message,
    );
  });

  it('a short private prefix behind other text is not refused: the ruling looks at the start', () => {
    expect(isPrivateExtendedKey('Savings uprv8Zgx more')).toBe(false);
  });

  it('is false for everything that is not a private key', () => {
    const values = [
      '',
      '   ',
      'Savings',
      ...Object.values(ADDRESSES),
      ...Object.values(EXTENDED_KEYS),
      ...[...PUBLIC_PREFIXES, ...MULTISIG_PREFIXES].map(short),
      privateKeyShapedRun('tprv', 99).slice(4),
    ];
    for (const value of values) {
      expect(isPrivateExtendedKey(value)).toBe(false);
    }
  });

  it('a quoted run blocks the address field on both chains', () => {
    for (const chainKey of CHAIN_KEYS) {
      const hint = addressHint(chainKey, `"${privateKeyShapedRun('uprv')}"`);
      expect(hint?.blocksSubmission).toBe(true);
      expect(hint?.message).toBe(PRIVATE_KEY_MESSAGE);
    }
  });

  it('the warning every field leads with is the one the address message starts with', () => {
    expect(PRIVATE_KEY_WARNING).toBe(
      'This is a private key. Never enter a private key here or anywhere else.',
    );
    expect(PRIVATE_KEY_MESSAGE.startsWith(PRIVATE_KEY_WARNING)).toBe(true);
  });

  it('answers a megabyte of ordinary text promptly', () => {
    const started = performance.now();
    expect(isPrivateExtendedKey('a'.repeat(1_000_000))).toBe(false);
    expect(isPrivateExtendedKey(`tpr${'a'.repeat(1_000_000)}`)).toBe(false);
    expect(performance.now() - started).toBeLessThan(1_000);
  });
});

describe('addressHint: a Kaspa prefix while Bitcoin is selected', () => {
  it.each([ADDRESSES.kasPrimary, ADDRESSES.kasSecondary, 'kaspatest:', 'kaspa:', 'kaspa:qq'])(
    'offers to switch to Kaspa for %s',
    (typed) => {
      const hint = addressHint('bitcoin', typed);

      expect(hint?.message).toMatch(/looks like a kaspa address/i);
      expect(hint?.switchTo).toBe('kaspa');
    },
  );

  it('recognises an upper-case prefix', () => {
    expect(addressHint('bitcoin', 'KASPATEST:QQ')?.switchTo).toBe('kaspa');
  });

  it('does not offer the switch once Kaspa is already selected', () => {
    expect(addressHint('kaspa', ADDRESSES.kasPrimary)).toBeUndefined();
  });
});

describe('addressHint: a Bitcoin shape while Kaspa is selected', () => {
  it.each([
    ADDRESSES.btcSegwit,
    ADDRESSES.btcLegacy,
    ADDRESSES.btcScript,
    ADDRESSES.btcRegtest,
    'bcrt1q',
    'BCRT1Q',
    'nX',
    'bc1q',
    'BC1Q',
    '1A',
    '3J',
  ])('offers to switch to Bitcoin for %s', (typed) => {
    const hint = addressHint('kaspa', typed);

    expect(hint?.message).toMatch(/looks like a bitcoin address/i);
    expect(hint?.switchTo).toBe('bitcoin');
  });

  it('does not offer the switch once Bitcoin is already selected', () => {
    expect(addressHint('bitcoin', ADDRESSES.btcSegwit)).toBeUndefined();
    expect(addressHint('bitcoin', ADDRESSES.btcLegacy)).toBeUndefined();
    expect(addressHint('bitcoin', ADDRESSES.btcScript)).toBeUndefined();
    expect(addressHint('bitcoin', ADDRESSES.btcRegtest)).toBeUndefined();
  });
});

describe('addressHint: Kaspa selected, no prefix', () => {
  it('says a Kaspa address includes its network prefix', () => {
    // A Kaspa payload pasted without `kaspatest:` - the prefix is part of the
    // checksum, so the server will refuse it, and this says why in advance.
    const payloadOnly = ADDRESSES.kasSecondary.slice('kaspatest:'.length);

    const hint = addressHint('kaspa', payloadOnly);

    expect(hint?.message).toMatch(/include their network prefix/i);
    expect(hint?.switchTo).toBeUndefined();
  });

  it('says so while the prefix itself is still being typed', () => {
    expect(addressHint('kaspa', 'kaspates')?.message).toMatch(/network prefix/i);
  });

  it('has nothing to add once a prefix is there', () => {
    expect(addressHint('kaspa', 'kaspatest:')).toBeUndefined();
    expect(addressHint('kaspa', 'kaspatest:qqnapngv')).toBeUndefined();
  });
});

describe('addressHint: never a verdict', () => {
  it('has nothing to say about a string it cannot place on Bitcoin', () => {
    // No checksum is checked here, by design: the server's is the only
    // verdict, and a second implementation would eventually disagree with it.
    expect(addressHint('bitcoin', 'not an address at all')).toBeUndefined();
    expect(addressHint('bitcoin', 'tb1qqqqqqq')).toBeUndefined();
  });

  it('returns a hint without echoing the address back', () => {
    // The hint is rendered on the page and could end up in a screenshot or an
    // error report; it describes the shape, it does not repeat the value.
    for (const [chainKey, typed] of [
      ['bitcoin', ADDRESSES.kasPrimary],
      ['kaspa', ADDRESSES.btcSegwit],
    ] as const) {
      expect(addressHint(chainKey, typed)?.message).not.toContain(typed);
    }
  });
});
