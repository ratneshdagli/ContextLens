const base = { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }
const I = (d, extra) => function Icon({ size = 18, ...p }) {
  return <svg {...base} width={size} height={size} {...p}>{d}{extra}</svg>
}

export const Today = I(<><path d="M4 5h16v14H4z" /><path d="M8 9h8M8 13h5" /></>)
export const Compass = I(<><circle cx="12" cy="12" r="9" /><path d="M15.5 8.5l-2 5-5 2 2-5z" /></>)
export const Bookmark = I(<path d="M6 4h12v17l-6-4-6 4z" />)
export const BookmarkFill = (p) => <svg {...base} {...p} fill="currentColor"><path d="M6 4h12v17l-6-4-6 4z" /></svg>
export const Gear = I(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" /></>)
export const Refresh = I(<><path d="M20 11a8 8 0 10-2.3 5.7" /><path d="M20 4v7h-7" /></>)
export const Search = I(<><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4.5 4.5" /></>)
export const ThumbUp = I(<path d="M7 11v9H4v-9zM7 11l4-7c1.5 0 2.5 1 2.2 2.6L12.7 9H19a2 2 0 012 2.3l-1.2 7A2 2 0 0117.8 20H7" />)
export const ThumbDown = I(<path d="M17 13V4h3v9zM17 13l-4 7c-1.5 0-2.5-1-2.2-2.6l.5-2.4H5a2 2 0 01-2-2.3l1.2-7A2 2 0 016.2 4H17" />)
export const Close = I(<path d="M6 6l12 12M18 6L6 18" />)
export const Back = I(<path d="M15 5l-7 7 7 7" />)
export const External = I(<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5" />)
export const Chat = I(<path d="M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z" />)
export const Send = I(<path d="M5 12l15-8-6 16-3-6.5z" />)
export const Moon = I(<path d="M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z" />)
export const Sun = I(<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5" /></>)
export const Check = I(<path d="M5 12.5l4.5 4.5L19 7" />)
export const Link = I(<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3A4 4 0 0011 18.7l1-1" />)
export const Flow = I(<><rect x="4" y="3" width="16" height="18" rx="3" /><path d="M9 9l3 3 3-3M9 15l3 3 3-3" /></>)
export const Upload = I(<><path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3" /></>)
export const Paper = I(<><path d="M6 3h9l4 4v14H6z" /><path d="M14 3v5h5M9 12h7M9 16h7" /></>)
export const Atlas = I(<><circle cx="6" cy="7" r="2.4" /><circle cx="18" cy="6" r="2.4" /><circle cx="12" cy="17" r="2.4" /><path d="M8 8.2l2.6 6.6M16.2 7.8l-3 7.2M8.4 7l7.2-.8" /></>)
export const Why = I(<><path d="M5 5v4a3 3 0 003 3h8a3 3 0 013 3v4" /><circle cx="5" cy="4" r="1.6" /><circle cx="19" cy="20" r="1.6" /></>)
export const Clock = I(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>)
export const Users = I(<><circle cx="9" cy="8" r="3.2" /><path d="M3 20a6 6 0 0112 0M16 5.2a3 3 0 010 5.6M18 20a5.5 5.5 0 00-3-5" /></>)
export const Wand = I(<><path d="M5 19L17 7l2 2L7 21z" /><path d="M14 4v3M12.5 5.5h3M19 12v2M18 13h2" /></>)
export const Flask = I(<><path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 001.3 2.2h12.4a1.5 1.5 0 001.3-2.2L14 9V3" /><path d="M7.5 15h9" /></>)
export const Quote = I(<><path d="M7 7h4v4c0 3-1.5 5-4 6M15 7h4v4c0 3-1.5 5-4 6" /></>)
export const Chevron = I(<path d="M6 9l6 6 6-6" />)
export const Plus = I(<path d="M12 5v14M5 12h14" />)
export const Play = (p) => <svg width={p.size || 18} height={p.size || 18} viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z" /></svg>
export const Flame = I(<path d="M12 3c1 4 5 5.5 5 10a5 5 0 01-10 0c0-2 1-3 2-4 .3 1.5 1 2 2 2-.5-3 0-5 1-8z" />)
export const Sparkle = I(<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z" />)
export const Mic = I(<><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21M9 21h6" /></>)
export const Stop = (p) => <svg width={p.size || 18} height={p.size || 18} viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5" /></svg>
export const Repeat = I(<><path d="M17 3l3 3-3 3" /><path d="M4 11V9a3 3 0 013-3h13" /><path d="M7 21l-3-3 3-3" /><path d="M20 13v2a3 3 0 01-3 3H4" /></>)
export const Sliders = I(<><path d="M4 7h9M17 7h3M4 17h3M11 17h9" /><circle cx="15" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></>)
export const Globe = I(<><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.6 2.6 3.9 5.6 3.9 9s-1.3 6.4-3.9 9c-2.6-2.6-3.9-5.6-3.9-9S9.4 5.6 12 3z" /></>)
export const Planet = I(<><circle cx="12" cy="12" r="5.5" /><ellipse cx="12" cy="12" rx="10" ry="3.6" transform="rotate(-25 12 12)" /></>)
export const Shield = I(<><path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6z" /><path d="M9 12l2.2 2.2L15.5 10" /></>)
export const List = I(<><path d="M8 6h12M8 12h12M8 18h12" /><circle cx="4" cy="6" r="1" /><circle cx="4" cy="12" r="1" /><circle cx="4" cy="18" r="1" /></>)
export const Repo = I(<><circle cx="6" cy="6" r="2.3" /><circle cx="6" cy="18" r="2.3" /><circle cx="18" cy="8" r="2.3" /><path d="M6 8.3V15.7M6 8.3C6 12 9 13 12 13h3.3M18 10.3V8" /></>)
export const Logo = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" fill="none"><circle cx="15" cy="15" r="6.5" stroke="#faf9f6" strokeWidth="2.8" /><path d="M20 20l6 6" stroke="#e2532b" strokeWidth="3.2" strokeLinecap="round" /></svg>
)
