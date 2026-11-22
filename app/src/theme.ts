import { createTheme } from "@mui/material/styles";

export const theme = createTheme({
  palette: {
    mode: "dark",
    background: {
      default: "#000000",
      paper: "#0B0B0D",
    },
    text: {
      primary: "#F5F5F7",
      secondary: "#A1A1A6",
    },
    primary: {
      main: "#FFB547",
      contrastText: "#0B0B0D",
    },
    success: { main: "#30D158" },
    warning: { main: "#FFD60A" },
    error: { main: "#FF453A" },
    divider: "rgba(255,255,255,0.08)",
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Display", "Hiragino Sans", sans-serif',
    h1: { fontSize: "28px", lineHeight: 1.1, fontWeight: 700, letterSpacing: "-0.03em" },
    h2: { fontSize: "19px", lineHeight: 1.2, fontWeight: 650, letterSpacing: "-0.02em" },
    body1: { fontSize: "13px", lineHeight: 1.5 },
    body2: { fontSize: "11px", lineHeight: 1.45 },
    button: { fontSize: "12px", fontWeight: 650, textTransform: "none", letterSpacing: "0.01em" },
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { borderRadius: 12, minHeight: 36, transition: "180ms ease-out" },
      },
    },
    MuiTextField: {
      defaultProps: { size: "small" },
    },
    MuiStepLabel: {
      styleOverrides: {
        label: { fontSize: "11px" },
      },
    },
  },
});

export const cardSx = {
  backgroundColor: "#0E0F12",
  borderRadius: "20px",
  border: "1px solid rgba(255,255,255,0.08)",
  boxShadow:
    "inset 0 48px 40px rgba(255,255,255,0.04), inset 0 -48px 40px rgba(0,0,0,0.55), 0 24px 60px rgba(0,0,0,0.6)",
};

export const accentTextSx = {
  background: "linear-gradient(110deg, #FFB547 5%, #FF7A59 52%, #B77CFF 94%)",
  backgroundClip: "text",
  WebkitBackgroundClip: "text",
  WebkitTextFillColor: "transparent",
};
