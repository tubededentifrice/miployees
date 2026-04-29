const ACCENTS: Record<string, string> = {
  A: "Å",
  B: "ß",
  C: "Ç",
  D: "Ð",
  E: "É",
  F: "Ƒ",
  G: "Ĝ",
  H: "Ĥ",
  I: "Ĩ",
  J: "Ĵ",
  K: "Ķ",
  L: "Ĺ",
  M: "Ṁ",
  N: "Ñ",
  O: "Ö",
  P: "Ṕ",
  Q: "Ǫ",
  R: "Ŕ",
  S: "Š",
  T: "Ŧ",
  U: "Ú",
  V: "Ṽ",
  W: "Ŵ",
  X: "Ẋ",
  Y: "Ý",
  Z: "Ž",
  a: "å",
  b: "ƀ",
  c: "ç",
  d: "ð",
  e: "é",
  f: "ƒ",
  g: "ĝ",
  h: "ĥ",
  i: "í",
  j: "ĵ",
  k: "ķ",
  l: "ĺ",
  m: "ṁ",
  n: "ñ",
  o: "ö",
  p: "ṕ",
  q: "ǫ",
  r: "ŕ",
  s: "š",
  t: "ŧ",
  u: "ú",
  v: "ṽ",
  w: "ŵ",
  x: "ẋ",
  y: "ý",
  z: "ž",
};

function accentStress(text: string): string {
  return Array.from(text, (char) => ACCENTS[char] ?? char).join("");
}

function inflate(text: string): string {
  const targetExtra = Math.ceil(text.length * 0.3);
  if (targetExtra <= 0) return text;
  const filler = " " + "~".repeat(Math.max(1, targetExtra - 1));
  return text + filler;
}

export function pseudolocalize(message: string): string {
  return message
    .split(/(\{[A-Za-z0-9_]+\})/g)
    .map((part) => (part.startsWith("{") && part.endsWith("}") ? part : inflate(accentStress(part))))
    .join("");
}
