import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";
import { resolveLocale, type SupportedLocale } from "@/i18n/locale";
import { createTranslator, type TFunction } from "@/i18n/translator";

interface I18nContextValue {
  locale: SupportedLocale;
  t: TFunction;
}

export interface I18nProviderProps {
  children: ReactNode;
  preferredLocale?: string | null;
  navigatorLanguages?: readonly string[];
  workspaceDefaultLocale?: string | null;
  search?: string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({
  children,
  preferredLocale,
  navigatorLanguages,
  workspaceDefaultLocale,
  search,
}: I18nProviderProps) {
  const locale = useMemo(
    () => resolveLocale({ search, preferredLocale, navigatorLanguages, workspaceDefaultLocale }),
    [navigatorLanguages, preferredLocale, search, workspaceDefaultLocale],
  );
  const translator = useMemo(() => createTranslator(locale), [locale]);
  const value = useMemo(() => ({ locale, t: translator }), [locale, translator]);

  useEffect(() => {
    const root = document.documentElement;
    const previousLang = root.lang;
    const previousLocale = root.dataset.locale;
    root.lang = locale;
    root.dataset.locale = locale;
    return () => {
      root.lang = previousLang;
      if (previousLocale === undefined) {
        delete root.dataset.locale;
      } else {
        root.dataset.locale = previousLocale;
      }
    };
  }, [locale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside <I18nProvider>");
  return value;
}

export function useT(): TFunction {
  return useI18n().t;
}
