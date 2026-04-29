export const enUSMessages = {
  "login.title": "Sign in with your passkey",
  "login.subtitle": "No passwords, ever. Tap once to unlock the house.",
  "login.passkeyButton": "Use passkey",
  "login.recoverLink": "Lost your device? Recover access \u2192",
  "login.firstTime": "First time here? Open the invite link your manager sent.",
  "login.inviteExampleLink": "See what accepting an invite looks like \u2192",
  "i18n.testGreeting": "Hello, {name}!",
} as const;

export type MessageKey = keyof typeof enUSMessages;

export interface MessageParamMap {
  "i18n.testGreeting": {
    name: string;
  };
}
