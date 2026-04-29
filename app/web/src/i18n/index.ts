export {
  DEFAULT_LOCALE,
  PSEUDO_LOCALE,
  SUPPORTED_LOCALES,
  resolveLocale,
  toSupportedLocale,
  type LocaleResolutionInput,
  type SupportedLocale,
} from "@/i18n/locale";
export { createTranslator, t, type TFunction } from "@/i18n/translator";
export { I18nProvider, useI18n, useT, type I18nProviderProps } from "@/i18n/I18nProvider";
export { enUSMessages, type MessageKey, type MessageParamMap } from "@/i18n/catalogs/en-US";
