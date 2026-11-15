// Icons: inline SVG, 24×24, drawn with the text color. Decorative: the buttons that hold them have their own label.
import type { ReactNode } from "react";

function Icon({ children, size = 18 }: { children: ReactNode; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}

export const FilmIcon = ({ size = 28 }: { size?: number }) => (
  <Icon size={size}><rect x="3" y="3" width="18" height="18" rx="3" /><path d="M7 3v18M17 3v18M3 9h4M3 15h4M17 9h4M17 15h4" /></Icon>
);
export const FolderIcon = () => <Icon><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></Icon>;
export const GearIcon = () => <Icon><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" /></Icon>;
export const CheckIcon = () => <Icon><path d="m5 12 5 5 9-10" /></Icon>;
export const ChevronDownIcon = () => <Icon><path d="m6 9 6 6 6-6" /></Icon>;
export const ChevronRightIcon = () => <Icon><path d="m9 6 6 6-6 6" /></Icon>;
export const ChevronLeftIcon = () => <Icon><path d="m15 6-6 6 6 6" /></Icon>;
export const CopyIcon = () => <Icon size={14}><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V6a2 2 0 0 1 2-2h8" /></Icon>;
export const CloseIcon = () => <Icon><path d="M6 6l12 12M18 6 6 18" /></Icon>;
export const AlertIcon = () => <Icon><path d="M12 3 2 20h20z" /><path d="M12 10v4M12 17.5v.5" /></Icon>;
export const MusicIcon = () => <Icon><path d="M9 18V5l11-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="17" cy="16" r="3" /></Icon>;
export const ImageIcon = () => <Icon><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="2" /><path d="m21 16-5-5-8 8" /></Icon>;
export const StopIcon = () => <Icon><rect x="6" y="6" width="12" height="12" rx="1" /></Icon>;
export const PlusIcon = () => <Icon><path d="M12 5v14M5 12h14" /></Icon>;
export const UndoIcon = () => <Icon><path d="M9 14 4 9l5-5" /><path d="M4 9h10a6 6 0 0 1 0 12h-3" /></Icon>;
export const RedoIcon = () => <Icon><path d="m15 14 5-5-5-5" /><path d="M20 9H10a6 6 0 0 0 0 12h3" /></Icon>;
export const EyeOffIcon = () => <Icon size={16}><path d="M3 3l18 18" /><path d="M10.6 6.2A10 10 0 0 1 12 6c6 0 9 6 9 6a15 15 0 0 1-3 3.7M6.6 7.6C4 9.3 3 12 3 12s3 6 9 6a9 9 0 0 0 3.2-.6" /></Icon>;
export const EyeIcon = () => <Icon size={16}><path d="M3 12s3-6 9-6 9 6 9 6-3 6-9 6-9-6-9-6z" /><circle cx="12" cy="12" r="3" /></Icon>;
export const TrashIcon = () => <Icon><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></Icon>;
