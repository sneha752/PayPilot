/**
 * Display formatting for backend-authoritative money strings.
 *
 * THIS MODULE PERFORMS NO ARITHMETIC. It never adds, multiplies, divides,
 * rounds, or converts a currency. The backend sends exact Decimal strings
 * ("34.99", "3192.84"); these functions only insert digit-group separators
 * and attach a currency symbol.
 *
 * Grouping is done by string manipulation on the integer part, so the
 * fractional part is carried through BYTE FOR BYTE. Parsing to a JS number
 * (and with it the risk of binary float drift or locale rounding changing
 * a displayed price) never happens.
 */

/** Symbols we are certain about. An unknown currency shows its ISO code
 * only — guessing a symbol would misrepresent the price. */
const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: "$",
  INR: "₹",
};

/** Currencies conventionally grouped 2,2,3 rather than 3,3,3. */
const INDIAN_GROUPED = new Set(["INR"]);

/** A decimal string we are willing to format: optional sign, digits, and
 * an optional fractional part. Anything else is rendered untouched. */
const DECIMAL_PATTERN = /^-?\d+(\.\d+)?$/;

function groupWestern(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  const last3 = digits.slice(-3);
  const rest = digits.slice(0, -3);
  // Remaining digits group in pairs: 1234567 -> 12,34,567
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${last3}`;
}

/**
 * Insert digit-group separators into an exact decimal string.
 *
 * The fractional part is preserved exactly as given — "3192.84" stays
 * two-decimal, "2500.00" keeps its trailing zeros. A value that is not a
 * plain decimal is returned unchanged rather than reinterpreted.
 */
export function groupDecimalString(value: string, currency?: string | null): string {
  const trimmed = value.trim();
  if (!DECIMAL_PATTERN.test(trimmed)) return trimmed;

  const negative = trimmed.startsWith("-");
  const unsigned = negative ? trimmed.slice(1) : trimmed;
  const [whole, fraction] = unsigned.split(".");

  const code = currency?.trim().toUpperCase();
  const grouped = code && INDIAN_GROUPED.has(code)
    ? groupIndian(whole)
    : groupWestern(whole);

  return `${negative ? "-" : ""}${grouped}${fraction ? `.${fraction}` : ""}`;
}

/**
 * Render a backend money string with its currency, or null when there is
 * no value to show.
 *
 * Returns null — never "0", never "₹0" — when the amount is missing, so
 * callers must decide what to display in the absence of a price rather
 * than accidentally showing a free product.
 */
export function formatMoney(
  amount: string | null | undefined,
  currency: string | null | undefined,
): string | null {
  if (amount === null || amount === undefined || amount.trim() === "") return null;

  const code = currency?.trim().toUpperCase() ?? "";
  const grouped = groupDecimalString(amount, code);
  const symbol = CURRENCY_SYMBOLS[code];

  if (symbol) return `${symbol}${grouped} ${code}`;
  // No symbol we trust: lead with the amount and name the currency.
  return code ? `${grouped} ${code}` : grouped;
}
