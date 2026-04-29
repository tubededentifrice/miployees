import { enUSMessages, type MessageKey, type MessageParamMap } from "@/i18n/catalogs/en-US";
import { DEFAULT_LOCALE, PSEUDO_LOCALE, type SupportedLocale } from "@/i18n/locale";
import { pseudolocalize } from "@/i18n/pseudo";

type Catalog = Record<MessageKey, string>;
type MissingKeyMode = "throw" | "return-key";
type MessageParamValue = string | number;
type MessageArgs<K extends MessageKey> = K extends keyof MessageParamMap
  ? [params: MessageParamMap[K]]
  : [];

export interface TFunction {
  <K extends MessageKey>(key: K, ...args: MessageArgs<K>): string;
}

interface TranslatorOptions {
  missingKeyMode?: MissingKeyMode;
}

function catalogFor(locale: SupportedLocale): Catalog {
  if (locale === PSEUDO_LOCALE) {
    return Object.fromEntries(
      Object.entries(enUSMessages).map(([key, value]) => [key, pseudolocalize(value)]),
    ) as Catalog;
  }
  return enUSMessages;
}

function missingKeyMode(): MissingKeyMode {
  return import.meta.env.PROD ? "return-key" : "throw";
}

function formatMessage(template: string, params: Record<string, MessageParamValue> = {}): string {
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name: string) => {
    const value = params[name];
    return value === undefined ? match : String(value);
  });
}

export function createTranslator(
  locale: SupportedLocale = DEFAULT_LOCALE,
  options: TranslatorOptions = {},
): TFunction {
  const catalog = catalogFor(locale);
  const onMissing = options.missingKeyMode ?? missingKeyMode();

  return ((key: MessageKey, ...args: [Record<string, MessageParamValue>?]) => {
    const template = catalog[key];
    if (template === undefined) {
      if (onMissing === "throw") throw new Error(`Missing i18n key: ${key}`);
      return key;
    }
    return formatMessage(template, args[0]);
  }) as TFunction;
}

export const t = createTranslator(DEFAULT_LOCALE);
