# GUI Layout Documentation

## Visual Structure

```
┌─────────────────────────────────────────────────────────────┐
│   DTS to AC-3 Converter                              [_][□][x]│
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ Input File                                              │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │ MKV File: [______________________________________] Browse│ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ Audio Streams                                           │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │                                                         │ │
│  │  Stream #1: DTS - 5.1 (1536 kb/s) [eng]               │ │
│  │  Stream #2: AC-3 - 2.0 (192 kb/s) [eng]               │ │
│  │                                                         │ │
│  │                                                         │ │
│  │                                                         │ │
│  │                                                         │ │
│  │                                                         │ │
│  │                                                    ┐    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ Output Settings                                         │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │ Output File: [______________________________] Browse     │ │
│  │ AC-3 Bitrate: [384k ▼]                                  │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  [████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]    │
│                                                              │
│  [ Convert ] [ Detect Audio ]                               │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Component Breakdown

### 1. **Input File Frame** (Top)
- **Label**: "MKV File:"
- **Text Entry**: Shows selected file path (read-only display)
- **Browse Button**: Opens file dialog to select .mkv files
  - Auto-generates output filename in output section
  - Auto-formats: `[original_name]_AC3.mkv`

### 2. **Audio Streams Frame** (Center - Expandable)
- **Text Widget**: Read-only text area displaying audio stream information
- **Scrollbar**: Vertical scrollbar for long content
- **Content Example**:
  ```
  Stream #1: DTS - 5.1 (1536 kb/s) [eng]
  Stream #2: AC-3 - 2.0 (192 kb/s) [eng]
  Stream #3: AAC - 2.0 (96 kb/s) [jpn]
  ```
- Shows:
  - Stream index
  - Audio codec name
  - Channel configuration
  - Bitrate
  - Language (if available)

### 3. **Output Settings Frame** (Below Audio)
- **Output File**:
  - Text Entry: Shows output file path
  - Browse Button: Save dialog for custom output location
  - Default: Same directory as input with `_AC3` suffix

- **AC-3 Bitrate**:
  - Dropdown Menu with options:
    - 192k (minimum, low quality)
    - 224k
    - 256k
    - 320k
    - 384k (default, recommended)
    - 448k (high quality)
    - 640k (maximum quality)

### 4. **Progress Bar** (Below Output)
- **Type**: Indeterminate progress bar (animated)
- **Behavior**:
  - Hidden/inactive when not converting
  - Animates during conversion
  - Stops when conversion completes
- **Visual**: Horizontal animated progress stripe

### 5. **Button Controls** (Bottom)
- **Convert Button**:
  - Disabled state: When no file selected or no audio detected
  - Active state: After audio streams detected
  - Click: Starts conversion process
  - Disables during conversion, re-enables after

- **Detect Audio Button**:
  - Always active
  - Click: Scans selected file for audio streams
  - Updates Audio Streams display
  - Enables Convert button if audio found

## Dimensions

- **Window Size**: 700×700 pixels
- **Resizable**: Yes (content adapts)
- **Minimum**: Not restricted, but 600×600 recommended

## Widget Layout Details

### Frame Hierarchy
```
root (Tk window)
├── main_frame (Frame, padx=10, pady=10)
│   ├── file_frame (LabelFrame: "Input File")
│   │   ├── Label: "MKV File:"
│   │   ├── Entry: file_entry
│   │   └── Button: Browse
│   ├── audio_frame (LabelFrame: "Audio Streams")
│   │   ├── Text: audio_text
│   │   └── Scrollbar: (vertical)
│   ├── output_frame (LabelFrame: "Output Settings")
│   │   ├── Label: "Output File:"
│   │   ├── Entry: output_entry
│   │   ├── Button: Browse
│   │   ├── Label: "AC-3 Bitrate:"
│   │   └── Combobox: bitrate_combo
│   ├── Progressbar: progress
│   └── button_frame (Frame)
│       ├── Button: Convert
│       └── Button: Detect Audio
```

### Layout Management
- **Packing**: Used for vertical stacking
- **Grid**: Used for Output Settings frame (2-column layout)
- **Expansion**: Audio Streams frame expands to fill available space

## User Interaction Flow

```
1. User launches application
   └─> FFmpeg initialization in background
   └─> Progress dialog shows (if downloading)
   └─> Main window appears when ready

2. User clicks "Browse" button
   └─> File dialog opens (MKV files filtered)
   └─> User selects file
   └─> File path appears in text entry
   └─> Output path auto-generated with _AC3 suffix

3. User clicks "Detect Audio" button
   └─> Text area shows "Detecting audio streams..."
   └─> Application scans file with FFmpeg
   └─> Audio streams list appears
   └─> Convert button becomes active

4. User optionally adjusts:
   └─> Output file path (Browse button)
   └─> AC-3 bitrate (Dropdown)

5. User clicks "Convert" button
   └─> Progress bar animates
   └─> Convert button disables
   └─> FFmpeg runs in background thread
   └─> Success/error message appears
   └─> Convert button re-enables

6. User clicks "Detect Audio" again for different file
   └─> Back to step 2
```

## State Management

### Button States
- **Convert Button**:
  - `disabled` on startup
  - `disabled` if no file selected
  - `normal` after audio detected
  - `disabled` during conversion
  - `normal` after conversion completes

- **Detect Audio Button**:
  - Always `normal` (always clickable)

### Progress Bar States
- `stopped` on startup
- `started` when conversion begins
- `stopped` when conversion completes

### Entry Fields
- **File Entry**: Read-only display (updated programmatically)
- **Output Entry**: Editable by user or Browse dialog
- Both show full file paths

## Color and Styling

- **Background**: Default tkinter gray
- **LabelFrames**: Groove relief, 2-pixel border
- **Text**: Default system font (monospace recommended for clarity)
- **Progress Bar**: Default blue/white animation

## Responsive Design

- **Vertical**: Audio Streams frame expands/contracts with window height
- **Horizontal**: Entry fields scale with window width
- **Buttons**: Stay at bottom, wrap if needed
- **Scrollbar**: Appears when Audio Streams exceed frame height

## Accessibility

- **Keyboard Navigation**: Tab through controls
- **Mouse**: All buttons clickable, dialogs standard
- **Messages**: Popup dialogs for errors and success
- **Text**: Clear labeling for all controls

## Known Limitations

1. **No Drag-Drop**: Would require tkinterdnd2 external library
2. **Single File**: No batch processing in current version
3. **No Preview**: No audio player embedded
4. **Progress Details**: Shows activity but not percentage
5. **Dark Mode**: Not supported in current tkinter version

## Future GUI Enhancements

- [ ] Drag-and-drop file support
- [ ] Real-time FFmpeg output details
- [ ] Percentage progress indicator
- [ ] Batch file queue
- [ ] Theme/dark mode support
- [ ] Audio preview player
- [ ] FFmpeg log viewer
- [ ] Conversion history
