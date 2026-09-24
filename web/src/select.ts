/** Wallet the dossier shows: an explicit pick, else the selected signal's whale, else the top certified whale. */
export function pickWallet(
  picked: string | null,
  signalWallet: string | null,
  whales: { wallet: string; certified: boolean }[],
): string | null {
  return picked ?? signalWallet ?? whales.find((w) => w.certified)?.wallet ?? null;
}
