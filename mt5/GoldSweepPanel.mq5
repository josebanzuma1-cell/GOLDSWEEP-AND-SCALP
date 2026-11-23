//+------------------------------------------------------------------+
//| GoldSweepPanel.mq5  (v3.1, S9)                                   |
//| GoldSweep's trading picture on the gold chart.                   |
//|                                                                  |
//| Main strategy (Common\Files\GoldSweep\signals.json, rewritten by |
//| the Python bot after every M5 bar): each signal's tag with its   |
//| grade (A+ / A / B), the trade box with SL / ENTRY / TP1-TP3, the |
//| "x" at the sweep, the result once closed; setups still FORMING   |
//| ("A+ possible" when the session's A+ rule can still be met); the |
//| nearest liquidity pools and open fair value gaps; and the        |
//| analysis box (Market, Levels, Setups, Scalper, Today, Brief).    |
//|                                                                  |
//| Scalper (Common\Files\GoldSweep\scalp.json, rewritten every      |
//| minute): the HFT step line, sweep "x" marks, each mode's own     |
//| layer (VWAP and ranges intraday, EMA and channel swing), every   |
//| scalp's box with its targets (TP1-TP5 for HFT's quick ladder),   |
//| TP hits and result, and the dashboard with the Quick Scalp line. |
//| It says STALE when the file stops updating.                      |
//|                                                                  |
//| Optional: GoldSweep's chart theme (your colours come back when   |
//| the indicator is removed) and alerts for new A+ setups.          |
//|                                                                  |
//| It never trades. Its only output is Common\Files\GoldSweep\      |
//| scalp_commands.json: the buttons (pause / resume, strategies,    |
//| scalper modes: HFT, Intraday and Swing each switch on or off, so |
//| one, two or all three run) write one command line there, which   |
//| the bot carries out like a Telegram command (D99, D113-D115).    |
//+------------------------------------------------------------------+
#property copyright   "GoldSweep"
#property version     "3.10"
#property description "GoldSweep: graded signals, forming A+ setups, levels, scalper layers, analysis."
#property description "Never trades. Reads Common\\Files\\GoldSweep\\signals.json and scalp.json."
#property indicator_chart_window
#property indicator_plots 0

enum ENUM_GS_GRADE
  {
   GS_GRADE_APLUS = 0, // A+ only
   GS_GRADE_A     = 1, // A and A+
   GS_GRADE_ALL   = 2  // every signal (B too)
  };

enum ENUM_GS_LAYERS
  {
   GS_LAYERS_SELECTED = 0, // the selected scalper mode only
   GS_LAYERS_ALL      = 1, // every scalper mode
   GS_LAYERS_NONE     = 2  // none
  };

input group "Files"
input string InpFile          = "GoldSweep\\signals.json";        // Main strategy (Common\Files)
input string InpScalpFile     = "GoldSweep\\scalp.json";          // Scalper (Common\Files)
input string InpCommandFile   = "GoldSweep\\scalp_commands.json"; // Button commands (Common\Files)
input int    InpRefreshSec    = 3;                // Read the files every N seconds
input int    InpStaleSec      = 180;              // Scalper file older than this: STALE

input group "Chart theme"
input bool   InpApplyTheme    = true;             // Apply GoldSweep's chart theme
input color  InpThemeBack     = C'14,17,22';      // Background (#0E1116)
input color  InpThemeBull     = C'233,238,242';   // Bull candles (#E9EEF2)
input color  InpThemeBear     = C'201,162,39';    // Bear candles (#C9A227)
input color  InpThemeText     = C'154,163,175';   // Scales and text
input bool   InpShowVolumes   = true;             // Tick-volume histogram

input group "Main strategy"
input bool          InpShowSignals = true;        // Signals: tags and trade boxes
input ENUM_GS_GRADE InpMinGrade    = GS_GRADE_A;  // Lowest grade drawn
input bool   InpShowForming   = true;             // Setups forming (before they trigger)
input bool   InpFormingAPlus  = false;            // ... only those that can be A+
input bool   InpShowPools     = true;             // Nearest liquidity pools
input bool   InpShowGaps      = true;             // Open fair value gaps
input int    InpBoxBars       = 24;               // Signal boxes: width in bars
input bool   InpShowPanel     = false;            // Old v1 corner box (bottom-left)

input group "Scalper"
input bool           InpShowScalper  = true;              // Scalps: boxes, tags, TP hits
input bool           InpShowStepLine = true;              // HFT step-trail line
input bool           InpShowSweeps   = true;              // Sweep "x" marks
input ENUM_GS_LAYERS InpModeLayers   = GS_LAYERS_SELECTED; // Mode layers (VWAP, EMA, ranges)
input int            InpScalpBoxBars = 40;                // Open scalp boxes: width in bars

input group "Panels"
input bool   InpShowDashboard = true;             // Scalper dashboard (bottom-left)
input bool   InpShowAnalysis  = true;             // Analysis box (top-left)
input int    InpAnalysisX     = 10;               // Analysis box: pixels from the left
input int    InpAnalysisY     = 70;               // Analysis box: pixels from the top
input int    InpDashboardY    = 20;               // Dashboard: pixels above the bottom
input bool   InpShowBrand     = true;             // "GoldSweep" mark (top-right)
input bool   InpShowButtons   = true;             // Switch buttons on the dashboard

input group "Alerts"
input bool   InpAlertAPlus    = true;             // Pop-up and sound: new A+ signal
input bool   InpAlertForming  = true;             // Pop-up and sound: new setup that can be A+
input bool   InpPushAlerts    = false;            // Also to the MT5 phone app
input string InpAlertSound    = "alert.wav";      // Sound file

input group "Colours"
input color  InpBuyColor      = C'46,204,113';    // BUY (#2ECC71)
input color  InpSellColor     = C'231,76,60';     // SELL (#E74C3C)
input color  InpStopColor     = C'231,76,60';     // Stop-loss line
input color  InpTargetColor   = C'154,209,168';   // Take-profit lines (#9AD1A8)
input color  InpGold          = C'212,175,55';    // GoldSweep gold (#D4AF37)
input color  InpEntryColor    = C'233,238,242';   // Entry line
input color  InpStepUp        = C'143,163,184';   // Step line, trend up (#8FA3B8)
input color  InpStepDown      = C'184,143,143';   // Step line, trend down
input color  InpPoolAbove     = C'150,90,90';     // Pools above price
input color  InpPoolBelow     = C'90,150,110';    // Pools below price
input color  InpGapBuy        = C'18,40,30';      // Bullish gap zone
input color  InpGapSell       = C'44,22,22';      // Bearish gap zone
input color  InpRiskFill      = C'58,24,28';      // Risk zone
input color  InpRewardFill    = C'20,50,36';      // Reward zone
input color  InpPanelBack     = C'14,17,22';      // Panels background

#define PREFIX  "GS_"
#define SPREFIX "GSS_"
#define DASH_HEIGHT 192   // the scalper box: six rows for TP1-TP5 (S9)
#define APREFIX "GSA_"
#define THEME   "GSTheme_"

datetime g_modified       = 0;
long     g_size           = -1;
datetime g_scalp_modified = 0;
long     g_scalp_size     = -1;
string   g_signals[];
int      g_signal_count   = 0;
string   g_scalp[];
int      g_scalp_count    = 0;
string   g_state          = "";
string   g_mode           = "off";
string   g_strategies     = "main";
long     g_updated_gmt    = 0;
int      g_clicks         = 0;
int      g_page           = 0;
long     g_pending_gmt    = 0;   // when a switch was sent (0: none waiting)
string   g_pending_text   = "";  // which one
string   g_seen[];
bool     g_first_load     = true;

string   g_pages[] = {"Market", "Levels", "Setups", "Scalper", "Today", "Brief"};

//+------------------------------------------------------------------+
int OnInit()
  {
   ThemeOnInit();
   EventSetTimer(MathMax(1, InpRefreshSec));
   Refresh(true);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, PREFIX);
   ObjectsDeleteAll(0, SPREFIX);
   ObjectsDeleteAll(0, APREFIX);
   if(reason == REASON_REMOVE)
      ThemeRestore();
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const int begin,
                const double &price[])
  {
   return(rates_total);
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   Refresh(false);
   ShowStale();
  }

//+------------------------------------------------------------------+
//| Buttons: page switches stay here; switches go to the bot          |
//+------------------------------------------------------------------+
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id != CHARTEVENT_OBJECT_CLICK)
      return;
   if(StringFind(sparam, APREFIX + "page_") == 0)
     {
      g_page = (int)StringToInteger(StringSubstr(sparam, StringLen(APREFIX + "page_")));
      ObjectSetInteger(0, sparam, OBJPROP_STATE, false);
      DrawAnalysis();
      ChartRedraw(0);
      return;
     }
   if(StringFind(sparam, SPREFIX + "cmd_") != 0)
      return;
   string command = StringSubstr(sparam, StringLen(SPREFIX + "cmd_"));
   StringReplace(command, "_", ":");
   if(command == "pause" && g_state != "active")
      command = "resume";
   // HFT, Intraday and Swing switch one mode on or off; the bot gets the whole list
   // ("scalper:hft,swing"), "Off" clears it (S9: one, two or all three modes).
   if(StringFind(command, "scalper:") == 0 && command != "scalper:off")
      command = "scalper:" + ToggleMode(g_mode, StringSubstr(command, 8));
   ObjectSetInteger(0, sparam, OBJPROP_STATE, false);
   if(SendCommand(command))
      Expect(command);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| After a click the buttons show the state just asked for, so a    |
//| second click on Pause sends Resume. scalp.json is rewritten once  |
//| a minute: until a copy written after the click arrives, its older |
//| state does not overwrite this one. The bot's next copy always has |
//| the last word (it says what really happened).                    |
//+------------------------------------------------------------------+
void Expect(const string command)
  {
   g_pending_gmt = (long)TimeGMT();
   g_pending_text = command;
   if(command == "pause")
      g_state = "paused";
   else
      if(command == "resume")
         g_state = "active";
      else
         if(StringFind(command, "mode:") == 0)
            g_strategies = StringSubstr(command, 5);
         else
            if(StringFind(command, "scalper:") == 0)
               g_mode = StringSubstr(command, 8);
   if(InpShowDashboard && g_scalp_count > 0)
      Dashboard(g_scalp[0]);
   DrawAnalysis();
  }

//+------------------------------------------------------------------+
//| The selected scalper modes, "off" or a list such as "hft,swing"   |
//+------------------------------------------------------------------+
bool HasMode(const string modes, const string mode)
  {
   string parts[];
   int count = StringSplit(modes, ',', parts);
   for(int i = 0; i < count; i++)
      if(parts[i] == mode)
         return(true);
   return(false);
  }

string ToggleMode(const string modes, const string mode)
  {
   string names[3] = {"hft", "intraday", "swing"};
   string result = "";
   for(int i = 0; i < 3; i++)
     {
      bool on = HasMode(modes, names[i]);
      if(names[i] == mode)
         on = !on;
      if(on)
         result += (result == "" ? "" : ",") + names[i];
     }
   return(result == "" ? "off" : result);
  }

//+------------------------------------------------------------------+
bool SendCommand(const string command)
  {
   int handle = FileOpen(InpCommandFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
     {
      Print("GoldSweep panel: could not write ", InpCommandFile, " (error ", GetLastError(), ")");
      return(false);
     }
   g_clicks++;
   long stamp = (long)TimeGMT();
   long id = stamp * 100 + (g_clicks % 100);
   FileWriteString(handle, "{\"id\":" + IntegerToString(id) + ",\"t\":" + IntegerToString(stamp) +
                   ",\"cmd\":\"" + command + "\"}\r\n");
   FileClose(handle);
   return(true);
  }

//+------------------------------------------------------------------+
//| The chart theme: saved per chart, restored when removed           |
//+------------------------------------------------------------------+
ENUM_CHART_PROPERTY_INTEGER g_theme_props[] =
  {
   CHART_COLOR_BACKGROUND, CHART_COLOR_FOREGROUND, CHART_COLOR_GRID, CHART_SHOW_GRID,
   CHART_COLOR_CANDLE_BULL, CHART_COLOR_CANDLE_BEAR, CHART_COLOR_CHART_UP,
   CHART_COLOR_CHART_DOWN, CHART_COLOR_CHART_LINE, CHART_SHOW_VOLUMES, CHART_COLOR_VOLUME,
   CHART_MODE
  };

string ThemeKey(const int index)
  {
   return(THEME + IntegerToString(ChartID()) + "_" + IntegerToString(index));
  }

void ThemeOnInit()
  {
   if(!InpApplyTheme)
     {
      ThemeRestore();
      return;
     }
   if(!GlobalVariableCheck(ThemeKey(0)))
      for(int i = 0; i < ArraySize(g_theme_props); i++)
         GlobalVariableSet(ThemeKey(i), (double)ChartGetInteger(0, g_theme_props[i]));
   ChartSetInteger(0, CHART_MODE, CHART_CANDLES);
   ChartSetInteger(0, CHART_COLOR_BACKGROUND, InpThemeBack);
   ChartSetInteger(0, CHART_COLOR_FOREGROUND, InpThemeText);
   ChartSetInteger(0, CHART_SHOW_GRID, false);
   ChartSetInteger(0, CHART_COLOR_GRID, InpThemeBack);
   ChartSetInteger(0, CHART_COLOR_CANDLE_BULL, InpThemeBull);
   ChartSetInteger(0, CHART_COLOR_CANDLE_BEAR, InpThemeBear);
   ChartSetInteger(0, CHART_COLOR_CHART_UP, InpThemeBull);
   ChartSetInteger(0, CHART_COLOR_CHART_DOWN, InpThemeBear);
   ChartSetInteger(0, CHART_COLOR_CHART_LINE, InpThemeText);
   ChartSetInteger(0, CHART_COLOR_VOLUME, C'70,78,90');
   ChartSetInteger(0, CHART_SHOW_VOLUMES, InpShowVolumes ? CHART_VOLUME_TICK : CHART_VOLUME_HIDE);
  }

void ThemeRestore()
  {
   if(!GlobalVariableCheck(ThemeKey(0)))
      return;
   for(int i = 0; i < ArraySize(g_theme_props); i++)
     {
      ChartSetInteger(0, g_theme_props[i], (long)GlobalVariableGet(ThemeKey(i)));
      GlobalVariableDel(ThemeKey(i));
     }
  }

//+------------------------------------------------------------------+
//| Value of "key":"..." in one line of the file                     |
//+------------------------------------------------------------------+
string JsonString(const string line, const string key)
  {
   string pattern = "\"" + key + "\":\"";
   int start = StringFind(line, pattern);
   if(start < 0)
      return("");
   start += StringLen(pattern);
   int stop = StringFind(line, "\"", start);
   if(stop < 0)
      return("");
   return(StringSubstr(line, start, stop - start));
  }

//+------------------------------------------------------------------+
//| Value of "key":number in one line of the file (0 when absent)    |
//+------------------------------------------------------------------+
double JsonNumber(const string line, const string key)
  {
   string pattern = "\"" + key + "\":";
   int start = StringFind(line, pattern);
   if(start < 0)
      return(0.0);
   start += StringLen(pattern);
   int stop = start;
   int length = StringLen(line);
   while(stop < length)
     {
      ushort c = StringGetCharacter(line, stop);
      if(c == ',' || c == '}' || c == ']')
         break;
      stop++;
     }
   return(StringToDouble(StringSubstr(line, start, stop - start)));
  }

datetime JsonTime(const string line, const string key)
  {
   return((datetime)(long)JsonNumber(line, key));
  }

//+------------------------------------------------------------------+
bool IsGoldChart()
  {
   string name = _Symbol;
   StringToUpper(name);
   return(StringFind(name, "XAU") >= 0 || StringFind(name, "GOLD") >= 0);
  }

//+------------------------------------------------------------------+
//| Read a file's lines when it changed; false when unchanged/absent |
//+------------------------------------------------------------------+
bool ReadLines(const string file, datetime &modified_seen, long &size_seen, const bool force,
               string &lines[], int &count, bool &missing)
  {
   missing = false;
   int handle = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON |
                         FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
     {
      missing = true;
      return(false);
     }
   datetime modified = (datetime)FileGetInteger(handle, FILE_MODIFY_DATE);
   long size = (long)FileSize(handle);
   if(!force && modified == modified_seen && size == size_seen)
     {
      FileClose(handle);
      return(false);
     }
   modified_seen = modified;
   size_seen = size;
   count = 0;
   ArrayResize(lines, 0);
   while(!FileIsEnding(handle))
     {
      string line = FileReadString(handle);
      if(StringLen(line) == 0)
         continue;
      ArrayResize(lines, count + 1, 512);
      lines[count] = line;
      count++;
     }
   FileClose(handle);
   return(true);
  }

//+------------------------------------------------------------------+
void Refresh(const bool force)
  {
   if(!IsGoldChart())
     {
      Label(APREFIX + "status", CORNER_LEFT_UPPER, 10, 30,
            "GoldSweep panel: attach it to the gold chart (XAUUSD / GOLD).", clrSilver, 10);
      ChartRedraw(0);
      return;
     }
   bool missing = false;
   bool changed = false;
   if(ReadLines(InpFile, g_modified, g_size, force, g_signals, g_signal_count, missing))
     {
      DrawMain();
      changed = true;
     }
   else
      if(missing && force)
         Label(APREFIX + "status", CORNER_LEFT_UPPER, InpAnalysisX, InpAnalysisY,
               "GoldSweep: waiting for " + InpFile + " (is the bot running?)", clrSilver, 9);
   bool scalp_missing = false;
   if(ReadLines(InpScalpFile, g_scalp_modified, g_scalp_size, force, g_scalp, g_scalp_count,
                scalp_missing))
     {
      DrawScalper();
      changed = true;
     }
   if(changed || force)
     {
      DrawAnalysis();
      Brand();
      g_first_load = false;
     }
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| Alerts, once per key; nothing on the first load                   |
//+------------------------------------------------------------------+
bool Seen(const string key)
  {
   for(int i = ArraySize(g_seen) - 1; i >= 0; i--)
      if(g_seen[i] == key)
         return(true);
   int size = ArraySize(g_seen);
   if(size >= 500)
     {
      ArrayRemove(g_seen, 0, 100);
      size = ArraySize(g_seen);
     }
   ArrayResize(g_seen, size + 1);
   g_seen[size] = key;
   return(false);
  }

void Notify(const string key, const string text)
  {
   if(Seen(key) || g_first_load)
      return;
   Alert(text);
   if(InpAlertSound != "")
      PlaySound(InpAlertSound);
   if(InpPushAlerts)
      SendNotification(text);
  }

//+------------------------------------------------------------------+
//| The main strategy                                                 |
//+------------------------------------------------------------------+
bool GradeShown(const string grade)
  {
   if(grade == "A+")
      return(true);
   if(grade == "A" || grade == "")
      return(InpMinGrade >= GS_GRADE_A);
   return(InpMinGrade >= GS_GRADE_ALL);
  }

void DrawMain()
  {
   ObjectsDeleteAll(0, PREFIX);
   if(g_signal_count == 0)
      return;
   string head = g_signals[0];
   int shown = 0;
   for(int i = 1; i < g_signal_count; i++)
     {
      string line = g_signals[i];
      string kind = JsonString(line, "k");
      if(kind == "" && StringFind(line, "\"id\":\"") >= 0)
        {
         string grade = JsonString(line, "grade");
         if(grade == "A+" && InpAlertAPlus)
            Notify("sig_" + JsonString(line, "id"),
                   "GoldSweep: A+ " + JsonString(line, "dir") + " setup " + JsonString(line, "id") +
                   " entry " + Price(JsonNumber(line, "entry")) + " SL " + Price(JsonNumber(line, "sl")) +
                   " (" + JsonString(line, "status") + ")");
         if(InpShowSignals && GradeShown(grade))
           {
            DrawSignal(line, i);
            shown++;
           }
        }
      else
         if(kind == "forming")
            DrawForming(line, i);
         else
            if(kind == "pool" && InpShowPools)
               DrawPool(line, i);
            else
               if(kind == "fvg" && InpShowGaps)
                  DrawGap(line, i);
     }
   if(InpShowPanel)
     {
      ShowLine(0, "GoldSweep " + JsonString(head, "mode") + " | trading " +
               JsonString(head, "state"), clrWhite);
      ShowLine(1, "Pressure: " + JsonString(head, "pressure") + " (estimate)", clrSilver);
      ShowLine(2, "News bias: " + DoubleToString(JsonNumber(head, "news_bias"), 2) + " | " +
               IntegerToString(shown) + " signal(s) in 24 h", clrSilver);
      ShowLine(3, "Updated " + JsonString(head, "updated_utc") + " | never trades", clrGray);
     }
  }

//+------------------------------------------------------------------+
//| A signal in the reference layout: tag, box, labels, "x", result  |
//+------------------------------------------------------------------+
void DrawSignal(const string line, const int row)
  {
   string id      = JsonString(line, "id");
   string dir     = JsonString(line, "dir");
   string status  = JsonString(line, "status");
   string grade   = JsonString(line, "grade");
   string tags    = JsonString(line, "tags");
   string book    = JsonString(line, "playbook");
   datetime start = JsonTime(line, "t");
   double score   = JsonNumber(line, "score");
   double entry   = JsonNumber(line, "entry");
   double sl      = JsonNumber(line, "sl");
   double tp1     = JsonNumber(line, "tp1");
   double tp2     = JsonNumber(line, "tp2");
   double tp3     = JsonNumber(line, "tp3");
   double pool    = JsonNumber(line, "pool");
   double sweep_x = JsonNumber(line, "sweep_x");
   datetime sweep_t = JsonTime(line, "sweep_t");
   datetime end   = JsonTime(line, "end");
   if(id == "" || start <= 0 || entry <= 0 || sl <= 0 || tp1 <= 0)
      return;
   bool buy = (dir == "BUY");
   bool aplus = (grade == "A+");
   color main = buy ? InpBuyColor : InpSellColor;
   int bar = PeriodSeconds(_Period);
   datetime finish = (status == "closed" && end > start) ? end : start + (datetime)(bar * InpBoxBars);
   double farthest = tp3 > 0 ? tp3 : (tp2 > 0 ? tp2 : tp1);
   string base = PREFIX + id + "_";

   Box(base + "risk", start, entry, finish, sl, InpRiskFill);
   Box(base + "reward", start, entry, finish, farthest, InpRewardFill);
   Level(base + "entry", start, finish, entry, InpEntryColor, STYLE_SOLID, 1, "ENTRY  " + Price(entry));
   Level(base + "sl", start, finish, sl, InpStopColor, STYLE_SOLID, 1, "SL  " + Price(sl));
   Level(base + "tp1", start, finish, tp1, InpTargetColor, STYLE_DASH, 1, "TP1  " + Price(tp1));
   if(tp2 > 0)
      Level(base + "tp2", start, finish, tp2, InpTargetColor, STYLE_DASH, 1, "TP2  " + Price(tp2));
   if(tp3 > 0)
      Level(base + "tp3", start, finish, tp3, InpTargetColor, STYLE_DASH, 1, "TP3  " + Price(tp3));
   if(sweep_x > 0 && sweep_t > 0)
      Text(base + "x", sweep_t, sweep_x, "x", InpGold, buy ? ANCHOR_UPPER : ANCHOR_LOWER, 11);
   else
      if(pool > 0)
         Level(base + "pool", start - (datetime)(bar * 6), start, pool, InpGold, STYLE_DOT, 1,
               "Swept " + JsonString(line, "pool_label"));

   string arrow = buy ? ShortToString(0x25B2) : ShortToString(0x25BC);
   string tag = arrow + " " + dir + " " + (grade == "" ? "" : grade + " ") + DoubleToString(score, 0);
   color tag_color = aplus ? InpGold : main;
   Text(base + "tag", start, sl, tag, tag_color, buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER,
        aplus ? 10 : 9);
   string detail = status;
   if(tags != "")
      detail += " | " + tags;
   if(book != "")
      detail += " | " + book;
   if(JsonNumber(line, "monitored") > 0)
      detail += " | A+ watch";
   double step = MathAbs(entry - sl) * 0.35;
   Text(base + "detail", start, buy ? sl - step : sl + step, detail, clrSilver,
        buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER, 7);
   if(status == "closed")
     {
      double r = JsonNumber(line, "r");
      Text(base + "result", finish, farthest, StringFormat("%+.2f R (%s)", r, JsonString(line, "exit")),
           r >= 0 ? InpBuyColor : InpSellColor, ANCHOR_RIGHT_LOWER, 9);
     }
  }

//+------------------------------------------------------------------+
//| A setup forming: a watch mark, not an order                       |
//+------------------------------------------------------------------+
void DrawForming(const string line, const int row)
  {
   bool aplus = (JsonNumber(line, "aplus") > 0);
   string dir = JsonString(line, "dir");
   string label = JsonString(line, "label");
   datetime since = JsonTime(line, "t");
   double level = JsonNumber(line, "level");
   double extreme = JsonNumber(line, "x");
   if(aplus && InpAlertForming)
      Notify("form_" + label + "_" + IntegerToString((long)since),
             "GoldSweep: A+ possible: " + dir + " setup forming at " + label + " " + Price(level) +
             " (" + JsonString(line, "stage") + ")");
   if(!InpShowForming || level <= 0 || since <= 0)
      return;
   if(InpFormingAPlus && !aplus)
      return;
   int bar = PeriodSeconds(_Period);
   string base = PREFIX + "forming_" + IntegerToString(row) + "_";
   color clr = aplus ? InpGold : (dir == "BUY" ? InpBuyColor : InpSellColor);
   string text = "FORMING " + dir + ": " + label + " " + Price(level) + " | " + JsonString(line, "text") +
                 (aplus ? " | A+ possible" : "");
   Level(base + "level", since, TimeCurrent() + (datetime)(bar * 6), level, clr, STYLE_DASHDOT, 1, text);
   if(extreme > 0)
      Text(base + "x", since, extreme, "x", clr, dir == "BUY" ? ANCHOR_UPPER : ANCHOR_LOWER, 11);
  }

void DrawPool(const string line, const int row)
  {
   double price = JsonNumber(line, "p");
   if(price <= 0)
      return;
   bool above = (JsonString(line, "side") == "above");
   int bar = PeriodSeconds(_Period);
   datetime from = TimeCurrent() - (datetime)(bar * 30);
   datetime to = TimeCurrent() + (datetime)(bar * 8);
   int width = (JsonNumber(line, "str") >= 0.7) ? 2 : 1;
   Level(PREFIX + "pool_" + IntegerToString(row), from, to, price, above ? InpPoolAbove : InpPoolBelow,
         STYLE_DOT, width, JsonString(line, "label") + "  " + Price(price));
  }

void DrawGap(const string line, const int row)
  {
   datetime start = JsonTime(line, "t");
   double top = JsonNumber(line, "top");
   double bottom = JsonNumber(line, "bot");
   if(start <= 0 || top <= 0 || bottom <= 0)
      return;
   bool buy = (JsonString(line, "dir") == "BUY");
   int bar = PeriodSeconds(_Period);
   string name = PREFIX + "fvg_" + IntegerToString(row);
   Box(name, start, top, TimeCurrent() + (datetime)(bar * 4), bottom, buy ? InpGapBuy : InpGapSell);
   Text(name + "_label", start, buy ? bottom : top, "FVG", buy ? InpPoolBelow : InpPoolAbove,
        buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER, 7);
  }

//+------------------------------------------------------------------+
//| The scalper                                                       |
//+------------------------------------------------------------------+
bool LayerShown(const string mode)
  {
   if(InpModeLayers == GS_LAYERS_NONE)
      return(false);
   if(InpModeLayers == GS_LAYERS_ALL)
      return(true);
   return(HasMode(g_mode, mode) || (mode == "" && HasMode(g_mode, "hft")));
  }

void DrawScalper()
  {
   ObjectsDeleteAll(0, SPREFIX);
   if(g_scalp_count == 0)
      return;
   string head = g_scalp[0];
   g_updated_gmt = (long)JsonNumber(head, "updated_gmt");
   // A copy written before the bot carried out the last click (it reads clicks every
   // 2 s) would show the old state: keep the clicked one until a later copy arrives.
   bool lagging = g_pending_gmt > 0 && g_updated_gmt < g_pending_gmt + 5 &&
                  (long)TimeGMT() < g_pending_gmt + 180;
   if(!lagging)
     {
      g_state = JsonString(head, "state");
      g_mode = JsonString(head, "mode");
      g_strategies = JsonString(head, "strategies");
      g_pending_gmt = 0;
      g_pending_text = "";
     }
   string series = "";
   datetime previous_time = 0;
   double previous_value = 0.0;
   int previous_up = 0;
   for(int i = 1; i < g_scalp_count; i++)
     {
      string line = g_scalp[i];
      string kind = JsonString(line, "k");
      string mode = JsonString(line, "mode");
      if(kind == "step" || kind == "line")
        {
         bool wanted = (kind == "step") ? (InpShowStepLine && LayerShown(mode == "" ? "hft" : mode))
                                        : LayerShown(mode);
         string key = kind + mode + JsonString(line, "name");
         datetime t = JsonTime(line, "t");
         double value = JsonNumber(line, "v");
         int up = (int)JsonNumber(line, "up");
         if(key == series && wanted && previous_time > 0 && t > previous_time)
           {
            color clr = (kind == "step") ? ((previous_up == 1) ? InpStepUp : InpStepDown)
                                         : LineColor(JsonString(line, "name"));
            if(kind == "step")
              {
               Segment(SPREFIX + "s_h" + IntegerToString(i), previous_time, previous_value, t,
                       previous_value, clr, 2, STYLE_SOLID);
               if(value != previous_value)
                  Segment(SPREFIX + "s_v" + IntegerToString(i), t, previous_value, t, value,
                          (up == 1) ? InpStepUp : InpStepDown, 2, STYLE_SOLID);
              }
            else
               Segment(SPREFIX + "l_" + IntegerToString(i), previous_time, previous_value, t, value,
                       clr, 1, StringFind(JsonString(line, "name"), "channel") >= 0 ? STYLE_DOT : STYLE_SOLID);
           }
         series = key;
         previous_time = t;
         previous_value = value;
         previous_up = up;
         continue;
        }
      series = "";
      if(kind == "x" && InpShowSweeps && LayerShown(mode == "" ? "hft" : mode))
         Text(SPREFIX + "x_" + IntegerToString(i), JsonTime(line, "t"), JsonNumber(line, "p"), "x",
              InpGold, JsonString(line, "s") == "high" ? ANCHOR_LOWER : ANCHOR_UPPER, 10);
      else
         if(kind == "box" && LayerShown(mode))
            DrawRange(line, i);
         else
            if(kind == "scalp" && InpShowScalper)
               DrawScalp(line);
            else
               if(kind == "hit" && InpShowScalper)
                  Text(SPREFIX + "hit_" + IntegerToString(i), JsonTime(line, "t"), JsonNumber(line, "p"),
                       JsonString(line, "tp"), InpTargetColor, ANCHOR_LOWER, 8);
     }
   if(InpShowDashboard)
      Dashboard(head);
  }

color LineColor(const string name)
  {
   if(name == "VWAP")
      return(InpGold);
   if(name == "EMA")
      return(C'120,170,220');
   return(C'110,120,135');
  }

void DrawRange(const string line, const int row)
  {
   datetime t1 = JsonTime(line, "t1");
   datetime t2 = JsonTime(line, "t2");
   double top = JsonNumber(line, "top");
   double bottom = JsonNumber(line, "bot");
   if(t1 <= 0 || t2 <= t1 || top <= 0)
      return;
   string name = SPREFIX + "box_" + IntegerToString(row);
   if(ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, top, t2, bottom))
     {
      ObjectSetInteger(0, name, OBJPROP_COLOR, C'60,66,78');
      ObjectSetInteger(0, name, OBJPROP_FILL, false);
      ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_DOT);
      ObjectSetInteger(0, name, OBJPROP_BACK, true);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
     }
   Text(name + "_label", t1, top, JsonString(line, "name"), C'110,120,135', ANCHOR_LEFT_LOWER, 7);
  }

void DrawScalp(const string line)
  {
   string id      = JsonString(line, "id");
   string dir     = JsonString(line, "dir");
   string mode    = JsonString(line, "mode");
   string where   = JsonString(line, "where");
   string status  = JsonString(line, "status");
   datetime start = JsonTime(line, "t");
   datetime end   = JsonTime(line, "end");
   double entry   = JsonNumber(line, "entry");
   double sl      = JsonNumber(line, "sl");
   double stop    = JsonNumber(line, "stop");
   double tp1     = JsonNumber(line, "tp1");
   double tp2     = JsonNumber(line, "tp2");
   double tp3     = JsonNumber(line, "tp3");
   double tp4     = JsonNumber(line, "tp4");
   double tp5     = JsonNumber(line, "tp5");
   double r       = JsonNumber(line, "r");
   if(id == "" || start <= 0 || entry <= 0 || sl <= 0 || tp1 <= 0)
      return;
   bool buy = (dir == "BUY");
   bool open = (status == "open");
   int bar = PeriodSeconds(_Period);
   datetime finish = (open || end <= start) ? start + (datetime)(bar * InpScalpBoxBars) : end;
   double farthest = tp5 > 0 ? tp5 : (tp4 > 0 ? tp4 : (tp3 > 0 ? tp3 : (tp2 > 0 ? tp2 : tp1)));
   string base = SPREFIX + id + "_";

   Box(base + "risk", start, entry, finish, sl, InpRiskFill);
   Box(base + "reward", start, entry, finish, farthest, InpRewardFill);
   Level(base + "entry", start, finish, entry, InpEntryColor, STYLE_SOLID, 1, "ENTRY  " + Price(entry));
   Level(base + "sl", start, finish, sl, InpStopColor, STYLE_SOLID, 1, "SL  " + Price(sl));
   Level(base + "tp1", start, finish, tp1, InpTargetColor, STYLE_DASH, 1, "TP1  " + Price(tp1));
   if(tp2 > 0)
      Level(base + "tp2", start, finish, tp2, InpTargetColor, STYLE_DASH, 1, "TP2  " + Price(tp2));
   if(tp3 > 0)
      Level(base + "tp3", start, finish, tp3, InpTargetColor, STYLE_DASH, 1, "TP3  " + Price(tp3));
   if(tp4 > 0)
      Level(base + "tp4", start, finish, tp4, InpTargetColor, STYLE_DASH, 1, "TP4  " + Price(tp4));
   if(tp5 > 0)
      Level(base + "tp5", start, finish, tp5, InpTargetColor, STYLE_DASH, 1, "TP5  " + Price(tp5));
   if(open && stop > 0 && MathAbs(stop - sl) > _Point)
      Level(base + "stop", start, finish, stop, InpStopColor, STYLE_DOT, 1, "stop  " + Price(stop));
   string arrow = buy ? ShortToString(0x25B2) : ShortToString(0x25BC);
   Text(base + "tag", start, sl, arrow + " " + dir + " " + mode + " " + where,
        buy ? InpBuyColor : InpSellColor, buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER, 9);
   string result = open ? StringFormat("open %+.2f R", r) : StringFormat("%+.2f R (%s)", r, JsonString(line, "exit"));
   Text(base + "result", finish, entry, result, (r >= 0) ? InpBuyColor : InpSellColor, ANCHOR_RIGHT_LOWER, 9);
  }

//+------------------------------------------------------------------+
//| The scalper's dashboard (bottom-left, the reference layout)       |
//+------------------------------------------------------------------+
void Dashboard(const string head)
  {
   int x = 10;
   int y = InpDashboardY;
   int width = 620;
   int height = DASH_HEIGHT;
   Panel(SPREFIX + "dash_back", CORNER_LEFT_LOWER, x, y + height, width, height);
   Panel(SPREFIX + "dash_header", CORNER_LEFT_LOWER, x, y + height, width, 24);
   ObjectSetInteger(0, SPREFIX + "dash_header", OBJPROP_BGCOLOR, C'40,34,14');

   string where = JsonString(head, "where");
   string status = JsonString(head, "status");
   string diamond = ShortToString(0x25C6);
   Label(SPREFIX + "dash_title", CORNER_LEFT_LOWER, x + 8, y + height - 4,
         diamond + " GoldSweep Scalper", InpGold, 10);
   Label(SPREFIX + "dash_badge", CORNER_LEFT_LOWER, x + 170, y + height - 4, where,
         (where == "DEMO") ? InpSellColor : clrSilver, 10);
   if(InpShowButtons)
     {
      Button(SPREFIX + "cmd_pause", CORNER_LEFT_LOWER, x + width - 92, y + height - 3, 84, 18,
             g_state == "active" ? "Pause" : "Resume", false);
      int bx = x + 8;
      int by = y + height - 28;
      Label(SPREFIX + "dash_trade", CORNER_LEFT_LOWER, bx, by - 2, "Trade:", clrSilver, 8);
      Button(SPREFIX + "cmd_mode_main", CORNER_LEFT_LOWER, bx + 44, by, 52, 17, "Main", g_strategies == "main");
      Button(SPREFIX + "cmd_mode_scalper", CORNER_LEFT_LOWER, bx + 100, by, 58, 17, "Scalper", g_strategies == "scalper");
      Button(SPREFIX + "cmd_mode_both", CORNER_LEFT_LOWER, bx + 162, by, 52, 17, "Both", g_strategies == "both");
      Label(SPREFIX + "dash_mode", CORNER_LEFT_LOWER, bx + 236, by - 2, "Scalper:", clrSilver, 8);
      Button(SPREFIX + "cmd_scalper_off", CORNER_LEFT_LOWER, bx + 290, by, 44, 17, "Off", g_mode == "off");
      Button(SPREFIX + "cmd_scalper_hft", CORNER_LEFT_LOWER, bx + 338, by, 44, 17, "HFT", HasMode(g_mode, "hft"));
      Button(SPREFIX + "cmd_scalper_intraday", CORNER_LEFT_LOWER, bx + 386, by, 64, 17, "Intraday", HasMode(g_mode, "intraday"));
      Button(SPREFIX + "cmd_scalper_swing", CORNER_LEFT_LOWER, bx + 454, by, 52, 17, "Swing", HasMode(g_mode, "swing"));
      if(g_pending_gmt > 0)
         Label(SPREFIX + "dash_sent", CORNER_LEFT_LOWER, bx + 512, by - 2, "sent: " + g_pending_text,
               clrSilver, 7);
      else
         ObjectDelete(0, SPREFIX + "dash_sent");
     }
   string side = JsonString(head, "trend");
   string side_name = (side == "UP") ? "BULLISH" : (side == "DOWN" ? "BEARISH" : "FLAT");
   Label(SPREFIX + "dash_status", CORNER_LEFT_LOWER, x + 8, y + height - 50,
         status + " | strategy=" + g_mode + " | side " + side_name + " | trading " + g_state,
         StatusColor(status), 9);
   Label(SPREFIX + "dash_quick", CORNER_LEFT_LOWER, x + 8, y + height - 66,
         "Quick Scalp -> " + JsonString(head, "signal") + ": " + JsonString(head, "signal_text"),
         JsonString(head, "signal") == "NONE" ? clrSilver : InpGold, 8);

   int row = y + height - 88;
   Label(SPREFIX + "m1", CORNER_LEFT_LOWER, x + 8, row, "Trend (" + JsonString(head, "tf") + "): " + side, clrSilver, 8);
   Label(SPREFIX + "m2", CORNER_LEFT_LOWER, x + 8, row - 16, "ATR: " + DoubleToString(JsonNumber(head, "atr"), 2), clrSilver, 8);
   Label(SPREFIX + "m3", CORNER_LEFT_LOWER, x + 8, row - 32, "Session: " + JsonString(head, "session"), clrSilver, 8);
   Label(SPREFIX + "m4", CORNER_LEFT_LOWER, x + 8, row - 48, "Spread: " + DoubleToString(JsonNumber(head, "spread"), 0) +
         " (median " + DoubleToString(JsonNumber(head, "spread_median"), 0) + ")", clrSilver, 8);
   string news = JsonString(head, "news");
   Label(SPREFIX + "m5", CORNER_LEFT_LOWER, x + 8, row - 64, "News: " + (news == "" ? "none ahead" : news), clrSilver, 8);

   string active = JsonString(head, "active");
   if(active == "")
      Label(SPREFIX + "t1", CORNER_LEFT_LOWER, x + 215, row, "No scalp open", clrSilver, 8);
   else
     {
      int hit = (int)JsonNumber(head, "active_hit");
      Label(SPREFIX + "t1", CORNER_LEFT_LOWER, x + 215, row, JsonString(head, "active_dir") + " " + active, clrWhite, 8);
      Label(SPREFIX + "t2", CORNER_LEFT_LOWER, x + 215, row - 16, "Entry " + Price(JsonNumber(head, "active_entry")) +
            "  stop " + Price(JsonNumber(head, "active_stop")), clrSilver, 8);
      // Targets two to a row (up to five: HFT's quick ladder, S9), then the R line.
      int line = 2;
      for(int k = 1; k <= 5; k += 2)
        {
         double first = JsonNumber(head, "active_tp" + IntegerToString(k));
         if(first <= 0)
            break;
         string text = Target(k, first, hit);
         double second = (k < 5) ? JsonNumber(head, "active_tp" + IntegerToString(k + 1)) : 0.0;
         if(second > 0)
            text += "  " + Target(k + 1, second, hit);
         line++;
         Label(SPREFIX + "t" + IntegerToString(line), CORNER_LEFT_LOWER, x + 215, row - 16 * (line - 1), text,
               InpTargetColor, 8);
        }
      line++;
      Label(SPREFIX + "t" + IntegerToString(line), CORNER_LEFT_LOWER, x + 215, row - 16 * (line - 1),
            StringFormat("%+.2f R | parts %s | %d min", JsonNumber(head, "active_r"),
                         JsonString(head, "active_children"), (int)JsonNumber(head, "active_min")), clrSilver, 8);
      for(int extra = line + 1; extra <= 6; extra++)
         ObjectDelete(0, SPREFIX + "t" + IntegerToString(extra));
     }

   Label(SPREFIX + "p1", CORNER_LEFT_LOWER, x + 420, row, "Today: " + Results(head, "today"), clrSilver, 8);
   Label(SPREFIX + "p2", CORNER_LEFT_LOWER, x + 420, row - 16, StringFormat("  won %.0f%% (BE %.0f%%)",
         100 * JsonNumber(head, "today_win"), 100 * JsonNumber(head, "today_be")), clrSilver, 8);
   Label(SPREFIX + "p3", CORNER_LEFT_LOWER, x + 420, row - 32, "30 days: " + Results(head, "d30"), clrSilver, 8);
   Label(SPREFIX + "p4", CORNER_LEFT_LOWER, x + 420, row - 48, StringFormat("  won %.0f%% (BE %.0f%%) PF %.2f",
         100 * JsonNumber(head, "d30_win"), 100 * JsonNumber(head, "d30_be"), JsonNumber(head, "d30_pf")), clrSilver, 8);
   Label(SPREFIX + "p5", CORNER_LEFT_LOWER, x + 420, row - 64, "Updated " + JsonString(head, "updated_utc"), clrGray, 8);
   ShowStale();
  }

string Target(const int number, const double price, const int hit)
  {
   return("TP" + IntegerToString(number) + " " + Price(price) + (hit >= number ? " ok" : ""));
  }

string Results(const string head, const string prefix)
  {
   return(StringFormat("%d scalps %+.2f R %+.2f", (int)JsonNumber(head, prefix + "_n"),
                       JsonNumber(head, prefix + "_r"), JsonNumber(head, prefix + "_net")));
  }

color StatusColor(const string status)
  {
   if(status == "READY")
      return(InpBuyColor);
   if(status == "IN TRADE")
      return(InpGold);
   if(status == "PAUSED")
      return(clrOrange);
   return(clrSilver);
  }

void ShowStale()
  {
   string name = SPREFIX + "dash_stale";
   if(!InpShowDashboard || g_updated_gmt <= 0)
      return;
   long age = (long)TimeGMT() - g_updated_gmt;
   if(age <= InpStaleSec)
     {
      ObjectDelete(0, name);
      return;
     }
   Label(name, CORNER_LEFT_LOWER, 300, InpDashboardY + DASH_HEIGHT - 4,
         StringFormat("STALE: no update for %d min (is the bot running?)", (int)(age / 60)),
         InpSellColor, 9);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| The analysis box (top-left): pages built from the bot's data      |
//+------------------------------------------------------------------+
void DrawAnalysis()
  {
   ObjectsDeleteAll(0, APREFIX);
   if(!InpShowAnalysis)
      return;
   int x = InpAnalysisX;
   int y = InpAnalysisY;
   int width = 470;
   int height = 196;
   Panel(APREFIX + "back", CORNER_LEFT_UPPER, x, y, width, height);
   Panel(APREFIX + "header", CORNER_LEFT_UPPER, x, y, width, 22);
   ObjectSetInteger(0, APREFIX + "header", OBJPROP_BGCOLOR, C'40,34,14');
   Label(APREFIX + "title", CORNER_LEFT_UPPER, x + 8, y + 4, ShortToString(0x25C6) + " GoldSweep analysis", InpGold, 9);
   for(int p = 0; p < ArraySize(g_pages); p++)
      Button(APREFIX + "page_" + IntegerToString(p), CORNER_LEFT_UPPER, x + 8 + p * 76, y + 26, 72, 18,
             g_pages[p], p == g_page);
   string lines[];
   PageLines(lines);
   for(int i = 0; i < ArraySize(lines) && i < 9; i++)
      Label(APREFIX + "line_" + IntegerToString(i), CORNER_LEFT_UPPER, x + 8, y + 50 + i * 16, lines[i],
            i == 0 ? clrWhite : clrSilver, 8);
  }

void Add(string &lines[], const string text)
  {
   int size = ArraySize(lines);
   ArrayResize(lines, size + 1);
   lines[size] = text;
  }

void AddWrapped(string &lines[], const string text, const int width)
  {
   string rest = text;
   while(StringLen(rest) > width)
     {
      int cut = width;
      while(cut > width / 2 && StringGetCharacter(rest, cut) != ' ')
         cut--;
      Add(lines, StringSubstr(rest, 0, cut));
      rest = "  " + StringSubstr(rest, cut + 1);
     }
   Add(lines, rest);
  }

void PageLines(string &lines[])
  {
   ArrayResize(lines, 0);
   string head = g_signal_count > 0 ? g_signals[0] : "";
   string scalp = g_scalp_count > 0 ? g_scalp[0] : "";
   if(g_page == 0)
     {
      Add(lines, "Market: " + Price(JsonNumber(head, "price")) + " | H1 " + JsonString(head, "h1") +
          StringFormat(" (%+.2f)", JsonNumber(head, "h1_score")) +
          StringFormat(" | news bias %+.2f", JsonNumber(head, "news_bias")));
      Add(lines, "Pressure: " + JsonString(head, "pressure") + " (estimate, not order flow)");
      Add(lines, "Session: " + JsonString(head, "session") + " | " + JsonString(head, "window"));
      string next = JsonString(head, "news_next");
      Add(lines, "Next release: " + (next == "" ? "none in the calendar" : next));
      string approved = JsonString(head, "approved");
      Add(lines, "A+ playbooks approved: " + (approved == "" ? "none (monitoring)" : approved));
      Add(lines, "Bot " + JsonString(head, "mode") + " | trading " + JsonString(head, "state") +
          " | strategies " + g_strategies + " | scalper " + g_mode);
      Add(lines, "Updated " + JsonString(head, "updated_utc"));
      return;
     }
   if(g_page == 1)
     {
      Add(lines, "Levels: liquidity pools nearest price, open gaps");
      for(int i = 1; i < g_signal_count; i++)
        {
         string line = g_signals[i];
         string kind = JsonString(line, "k");
         if(kind == "pool")
            Add(lines, StringFormat("%-5s %-28s %s", JsonString(line, "side"), JsonString(line, "label"),
                                    Price(JsonNumber(line, "p"))));
         else
            if(kind == "fvg")
               Add(lines, "gap   " + JsonString(line, "dir") + " " + Price(JsonNumber(line, "bot")) + " - " +
                   Price(JsonNumber(line, "top")));
        }
      if(ArraySize(lines) == 1)
         Add(lines, "No levels yet (they come with the next M5 bar).");
      return;
     }
   if(g_page == 2)
     {
      Add(lines, "Setups: forming now, then the latest signals");
      int forming = 0;
      for(int i = 1; i < g_signal_count; i++)
        {
         string line = g_signals[i];
         if(JsonString(line, "k") != "forming")
            continue;
         forming++;
         AddWrapped(lines, (JsonNumber(line, "aplus") > 0 ? "A+ possible: " : "forming: ") + JsonString(line, "dir") +
                    " at " + JsonString(line, "label") + " " + Price(JsonNumber(line, "level")) + ", " +
                    JsonString(line, "text"), 74);
        }
      if(forming == 0)
         Add(lines, "Nothing forming right now.");
      int listed = 0;
      for(int i = g_signal_count - 1; i >= 1 && listed < 4; i--)
        {
         string line = g_signals[i];
         if(JsonString(line, "k") != "" || StringFind(line, "\"id\":\"") < 0)
            continue;
         listed++;
         Add(lines, TimeToString(JsonTime(line, "t"), TIME_MINUTES) + "  " + JsonString(line, "dir") + " " +
             JsonString(line, "grade") + " " + DoubleToString(JsonNumber(line, "score"), 0) + "  " +
             JsonString(line, "status") + "  " + JsonString(line, "tags"));
        }
      return;
     }
   if(g_page == 3)
     {
      Add(lines, "Scalper: " + JsonString(scalp, "status") + " | mode " + g_mode + " | " + JsonString(scalp, "where"));
      AddWrapped(lines, "Quick Scalp -> " + JsonString(scalp, "signal") + ": " + JsonString(scalp, "signal_text"), 74);
      AddWrapped(lines, JsonString(scalp, "m_hft"), 74);
      AddWrapped(lines, JsonString(scalp, "m_intraday"), 74);
      AddWrapped(lines, JsonString(scalp, "m_swing"), 74);
      return;
     }
   if(g_page == 4)
     {
      Add(lines, "Today (last 24 h, after costs)");
      Add(lines, StringFormat("Main strategy: %d trades, %+.2f R", (int)JsonNumber(head, "today_n"),
                              JsonNumber(head, "today_r")));
      Add(lines, "Scalper " + g_mode + ": " + Results(scalp, "today"));
      Add(lines, "Scalper 30 days: " + Results(scalp, "d30"));
      Add(lines, StringFormat("  won %.0f%%, break-even %.0f%%, PF %.2f", 100 * JsonNumber(scalp, "d30_win"),
                              100 * JsonNumber(scalp, "d30_be"), JsonNumber(scalp, "d30_pf")));
      Add(lines, "Past results do not predict future ones; every trade can lose.");
      return;
     }
   string title = JsonString(head, "brief_title");
   if(title == "")
     {
      Add(lines, "Brief: no scenario brief yet (they come before the sessions and releases).");
      return;
     }
   Add(lines, "Brief: " + title + " (" + JsonString(head, "brief_time") + ")");
   for(int i = 1; i <= 6; i++)
     {
      string text = JsonString(head, "brief_" + IntegerToString(i));
      if(text != "")
         AddWrapped(lines, text, 74);
     }
  }

//+------------------------------------------------------------------+
//| Small drawing helpers                                             |
//+------------------------------------------------------------------+
void Brand()
  {
   string name = APREFIX + "brand";
   if(!InpShowBrand)
      return;
   Label(name, CORNER_RIGHT_UPPER, 90, 6, ShortToString(0x25C6) + " GoldSweep", InpGold, 9);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_RIGHT_UPPER);
  }

void Panel(const string name, const ENUM_BASE_CORNER corner, const int x, const int y, const int width,
           const int height)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, corner);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, width);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, height);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, InpPanelBack);
   ObjectSetInteger(0, name, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, name, OBJPROP_COLOR, InpGold);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

void Label(const string name, const ENUM_BASE_CORNER corner, const int x, const int y, const string text,
           const color clr, const int size)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, corner);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetString(0, name, OBJPROP_FONT, "Segoe UI");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

void Button(const string name, const ENUM_BASE_CORNER corner, const int x, const int y, const int width,
            const int height, const string text, const bool active)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, corner);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, width);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, height);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, active ? C'90,74,20' : C'34,38,46');
   ObjectSetInteger(0, name, OBJPROP_BORDER_COLOR, active ? InpGold : C'60,66,78');
   ObjectSetInteger(0, name, OBJPROP_COLOR, active ? clrWhite : InpGold);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_STATE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

string Price(const double value)
  {
   return(DoubleToString(value, _Digits));
  }

void Box(const string name, const datetime t1, const double p1,
         const datetime t2, const double p2, const color fill)
  {
   if(!ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, p1, t2, p2))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, fill);
   ObjectSetInteger(0, name, OBJPROP_FILL, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

void Segment(const string name, const datetime t1, const double p1, const datetime t2,
             const double p2, const color clr, const int width, const ENUM_LINE_STYLE style)
  {
   if(!ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

void Level(const string name, const datetime t1, const datetime t2, const double price,
           const color clr, const ENUM_LINE_STYLE style, const int width, const string text)
  {
   if(!ObjectCreate(0, name, OBJ_TREND, 0, t1, price, t2, price))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, text);
   Text(name + "_text", t2, price, text, clr, ANCHOR_LEFT, 8);
  }

void Text(const string name, const datetime t, const double price, const string text,
          const color clr, const ENUM_ANCHOR_POINT anchor, const int size)
  {
   if(!ObjectCreate(0, name, OBJ_TEXT, 0, t, price))
      return;
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Segoe UI");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
//| The old v1 corner box (off by default; the analysis box replaces |
//| it)                                                               |
//+------------------------------------------------------------------+
void ShowLine(const int row, const string text, const color clr)
  {
   string name = PREFIX + "panel_" + IntegerToString(row);
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 10);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 210 + (3 - row) * 16);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }
//+------------------------------------------------------------------+
