/**
 * Design tokens — replicated from OpenCode's default dark theme.
 *
 * Values come from packages/theme/src/tui/assets/v2/opencode.json in the
 * OpenCode repo (neutral scale + accent hues).  Field names are flattened
 * (textBase instead of text.base) because the consumer is a plain object,
 * not a token resolver; the resolver can land later if themes need it.
 */

export interface Theme {
  textBase: string;
  textMuted: string;
  backgroundBase: string;
  backgroundRaised: string;
  backgroundRaisedHigh: string;
  backgroundRaisedMax: string;
  actionPrimaryFocused: string;
  actionPrimarySelected: string;
  feedbackError: string;
  feedbackWarning: string;
  feedbackSuccess: string;
  feedbackInfo: string;
  borderBase: string;
  scrollbarBase: string;
  diffAdded: string;
  diffRemoved: string;
  diffAddedBg: string;
  diffRemovedBg: string;
  hueAccent: string;
  hueInteractive: string;
  /** Agent accent used for the prompt border (opencode's default agent hue). */
  agentColor: string;
}

export const darkTheme: Theme = {
  textBase: "#eeeeee",
  textMuted: "#808080",
  backgroundBase: "#0a0a0a",
  backgroundRaised: "#141414",
  backgroundRaisedHigh: "#1e1e1e",
  backgroundRaisedMax: "#4c4c4c",
  actionPrimaryFocused: "#fab283",
  actionPrimarySelected: "#fab283",
  feedbackError: "#e06c75",
  feedbackWarning: "#f5a742",
  feedbackSuccess: "#7fd88f",
  feedbackInfo: "#56b6c2",
  borderBase: "#484848",
  scrollbarBase: "#606060",
  diffAdded: "#4fd6be",
  diffRemoved: "#c53b53",
  diffAddedBg: "#20303b",
  diffRemovedBg: "#37222c",
  hueAccent: "#9d7cd8",
  hueInteractive: "#fab283",
  agentColor: "#fab283",
};

export const lightTheme: Theme = {
  textBase: "#1a1a1a",
  textMuted: "#707070",
  backgroundBase: "#ffffff",
  backgroundRaised: "#f5f5f5",
  backgroundRaisedHigh: "#ececec",
  backgroundRaisedMax: "#d4d4d4",
  actionPrimaryFocused: "#d97848",
  actionPrimarySelected: "#d97848",
  feedbackError: "#c53b53",
  feedbackWarning: "#b8791f",
  feedbackSuccess: "#3d9e52",
  feedbackInfo: "#2a8f9e",
  borderBase: "#b8b8b8",
  scrollbarBase: "#a0a0a0",
  diffAdded: "#1f9d85",
  diffRemoved: "#c53b53",
  diffAddedBg: "#e3f5f1",
  diffRemovedBg: "#fbe9ed",
  hueAccent: "#7a5fb8",
  hueInteractive: "#d97848",
  agentColor: "#d97848",
};

/**
 * The active theme.  Own copy (not the darkTheme constant) so
 * ``applyTheme`` can swap values without mutating the source tables.
 * Live switching (/themes dialog) lands in M3 and will move this into a
 * Solid context; M1 only applies the detected mode once at startup.
 */
export const theme: Theme = { ...darkTheme };
