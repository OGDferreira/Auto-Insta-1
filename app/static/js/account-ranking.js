// Missing data always follows real values, including a genuine zero.
export function sortAccounts(accounts, order = "desc") {
  return accounts.map((account, index) => ({ account, index })).sort((a, b) => {
    const byName = () => String(a.account.account || "").localeCompare(String(b.account.account || ""), "pt-BR") || a.index - b.index;
    if (order === "name") return byName();
    const missing = value => value === null || value === undefined || value === "" || !Number.isFinite(Number(value));
    const aMissing = missing(a.account.engagement), bMissing = missing(b.account.engagement);
    if (aMissing !== bMissing) return aMissing ? 1 : -1;
    if (aMissing) return byName();
    const difference = Number(a.account.engagement) - Number(b.account.engagement);
    return (order === "asc" ? difference : -difference) || byName();
  }).map(row => row.account);
}
