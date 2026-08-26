const MAX_BASE_BLOCK_SIZE = 10_000n;
const MAX_APPROXIMATION_DENOMINATOR = 10_000n;

interface Rational {
  numerator: bigint;
  denominator: bigint;
}

function absolute(value: bigint): bigint {
  return value < 0n ? -value : value;
}

function gcd(left: bigint, right: bigint): bigint {
  let a = absolute(left);
  let b = absolute(right);
  while (b !== 0n) [a, b] = [b, a % b];
  return a;
}

function reduced(numerator: bigint, denominator: bigint): Rational {
  if (denominator <= 0n) throw new Error("ratio denominator must be positive");
  const divisor = gcd(numerator, denominator);
  return {
    numerator: numerator / divisor,
    denominator: denominator / divisor,
  };
}

function decimalNumberAsRational(value: number): Rational | null {
  if (!Number.isFinite(value) || value <= 0) return null;
  const text = value.toString().toLowerCase();
  const [mantissa, exponentText = "0"] = text.split("e");
  const exponent = Number(exponentText);
  if (!Number.isInteger(exponent)) return null;
  const point = mantissa.indexOf(".");
  const fractionalDigits = point < 0 ? 0 : mantissa.length - point - 1;
  const digits = mantissa.replace(".", "");
  if (!/^\d+$/.test(digits)) return null;
  const decimalShift = exponent - fractionalDigits;
  const coefficient = BigInt(digits);
  return decimalShift >= 0
    ? reduced(coefficient * (10n ** BigInt(decimalShift)), 1n)
    : reduced(coefficient, 10n ** BigInt(-decimalShift));
}

function divide(left: Rational, right: Rational): Rational {
  return reduced(
    left.numerator * right.denominator,
    left.denominator * right.numerator,
  );
}

function closerTo(
  original: Rational,
  left: Rational,
  right: Rational,
): Rational {
  const leftDifference = absolute(
    left.numerator * original.denominator
      - original.numerator * left.denominator,
  );
  const rightDifference = absolute(
    right.numerator * original.denominator
      - original.numerator * right.denominator,
  );
  // The common original denominator cancels. Match Python Fraction's tie
  // behavior by preferring the second convergent when distances are equal.
  return rightDifference * left.denominator <= leftDifference * right.denominator
    ? right
    : left;
}

/** Port of Fraction.limit_denominator for a positive exact rational. */
function limitDenominator(value: Rational, maximum: bigint): Rational {
  if (value.denominator <= maximum) return value;
  let p0 = 0n;
  let q0 = 1n;
  let p1 = 1n;
  let q1 = 0n;
  let numerator = value.numerator;
  let denominator = value.denominator;
  for (;;) {
    const quotient = numerator / denominator;
    const q2 = q0 + quotient * q1;
    if (q2 > maximum) break;
    [p0, q0, p1, q1] = [p1, q1, p0 + quotient * p1, q2];
    [numerator, denominator] = [
      denominator,
      numerator - quotient * denominator,
    ];
  }
  const multiplier = (maximum - q0) / q1;
  const bounded = reduced(p0 + multiplier * p1, q0 + multiplier * q1);
  const convergent = reduced(p1, q1);
  return closerTo(value, bounded, convergent);
}

/**
 * Convert proportional allocation weights to the smallest bounded integer
 * quota. Common rescaling does not change the result.
 */
export function normalizedAllocationWeights(values: number[]): number[] | null {
  if (!Array.isArray(values) || values.length < 2 || values.length > 100) {
    return null;
  }
  const exact = values.map(decimalNumberAsRational);
  if (exact.some((value) => value === null)) return null;
  const rationals = exact as Rational[];
  const scale = rationals.reduce((smallest, value) => (
    value.numerator * smallest.denominator
      < smallest.numerator * value.denominator ? value : smallest
  ));
  const relative = rationals.map((value) => divide(value, scale));
  if (relative.some((value) => (
    value.numerator > MAX_BASE_BLOCK_SIZE * value.denominator
  ))) return null;
  const approximated = relative.map((value) => (
    limitDenominator(value, MAX_APPROXIMATION_DENOMINATOR)
  ));
  const commonDenominator = approximated.reduce((current, value) => (
    (current / gcd(current, value.denominator)) * value.denominator
  ), 1n);
  const whole = approximated.map((value) => (
    value.numerator * (commonDenominator / value.denominator)
  ));
  const commonDivisor = whole.reduce((current, value) => gcd(current, value));
  const normalized = whole.map((value) => value / commonDivisor);
  const total = normalized.reduce((sum, value) => sum + value, 0n);
  if (total > MAX_BASE_BLOCK_SIZE) return null;
  return normalized.map(Number);
}
