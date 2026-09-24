import { describe, expect, it } from 'vitest';
import { pickWallet } from './select';

const whales = [{ wallet: '0xtop', certified: true }, { wallet: '0xwatch', certified: false }];

describe('pickWallet', () => {
  it('prefers an explicit pick, then the selected signal', () => {
    expect(pickWallet('0xpick', '0xsig', whales)).toBe('0xpick');
    expect(pickWallet(null, '0xsig', whales)).toBe('0xsig');
  });

  it('falls back to the top certified whale so the dossier is never empty', () => {
    expect(pickWallet(null, null, whales)).toBe('0xtop');
    expect(pickWallet(null, null, [{ wallet: '0xwatch', certified: false }])).toBeNull();
    expect(pickWallet(null, null, [])).toBeNull();
  });
});
