//+------------------------------------------------------------------+
//| GoldSweepPanel.mq5                                               |
//| Draws GoldSweep's recent signals on the gold chart.              |
//|                                                                  |
//| It reads Common\Files\GoldSweep\signals.json, which the Python   |
//| bot rewrites after every M5 bar, and draws for each signal: a    |
//| BUY/SELL label with its score, the entry, stop-loss and TP1-TP3  |
//| lines, a shaded risk box and reward box, and the swept level.    |
//| A corner box shows the mode, the trading state, the buyer/seller |
//| pressure estimate and the news bias.                             |
//|                                                                  |
//| Read-only: this indicator never trades and never writes files.   |
//+------------------------------------------------------------------+
#property copyright   "GoldSweep"
#property version     "1.00"
#property description "Draws GoldSweep signals: entry, SL, TP1-TP3, risk and reward boxes, swept level."
#property description "Read-only: it never trades. Data: Common\\Files\\GoldSweep\\signals.json"
#property indicator_chart_window
#property indicator_plots 0

input string InpFile        = "GoldSweep\\signals.json"; // File in MT5 Common\Files
input int    InpRefreshSec  = 5;               // Read the file every N seconds
input int    InpBoxBars     = 24;              // Width of the boxes, in bars of this chart
input bool   InpShowPanel   = true;            // Show the corner box
input color  InpBuyColor    = clrDodgerBlue;   // BUY label and entry
input color  InpSellColor   = clrOrangeRed;    // SELL label and entry
input color  InpStopColor   = clrRed;          // Stop-loss line
input color  InpTargetColor = clrLimeGreen;    // Take-profit lines
input color  InpPoolColor   = clrGold;         // Swept liquidity level
input color  InpRiskFill    = C'90,30,30';     // Risk box
input color  InpRewardFill  = C'25,70,40';     // Reward box

#define PREFIX "GS_"

datetime g_modified = 0;
long     g_size     = -1;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(MathMax(1, InpRefreshSec));
   Refresh(true);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, PREFIX);
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

//+------------------------------------------------------------------+
bool IsGoldChart()
  {
   string name = _Symbol;
   StringToUpper(name);
   return(StringFind(name, "XAU") >= 0 || StringFind(name, "GOLD") >= 0);
  }

//+------------------------------------------------------------------+
//| Read the file when it has changed, then redraw                   |
//+------------------------------------------------------------------+
void Refresh(const bool force)
  {
   if(!IsGoldChart())
     {
      ShowStatus("GoldSweep panel: attach it to the gold chart (XAUUSD / GOLD).", clrSilver);
      return;
     }
   int handle = FileOpen(InpFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON |
                         FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
     {
      ShowStatus("GoldSweep panel: waiting for " + InpFile + " (start GoldSweep).", clrSilver);
      return;
     }
   datetime modified = (datetime)FileGetInteger(handle, FILE_MODIFY_DATE);
   long size = (long)FileSize(handle);
   if(!force && modified == g_modified && size == g_size)
     {
      FileClose(handle);
      return;
     }
   g_modified = modified;
   g_size = size;
   string lines[];
   int count = 0;
   while(!FileIsEnding(handle))
     {
      string line = FileReadString(handle);
      if(StringLen(line) == 0)
         continue;
      ArrayResize(lines, count + 1);
      lines[count] = line;
      count++;
     }
   FileClose(handle);
   Draw(lines, count);
  }

//+------------------------------------------------------------------+
void Draw(string &lines[], const int count)
  {
   ObjectsDeleteAll(0, PREFIX);
   if(count == 0)
     {
      ChartRedraw(0);
      return;
     }
   string head = lines[0];
   int shown = 0;
   for(int i = 1; i < count; i++)
     {
      if(StringFind(lines[i], "\"id\":\"") < 0)
         continue;
      DrawSignal(lines[i]);
      shown++;
     }
   if(InpShowPanel)
     {
      ShowLine(0, "GoldSweep " + JsonString(head, "mode") + " | trading " +
               JsonString(head, "state"), clrWhite);
      ShowLine(1, "Pressure: " + JsonString(head, "pressure") + " (estimate)", clrSilver);
      ShowLine(2, "News bias: " + DoubleToString(JsonNumber(head, "news_bias"), 2) + " | " +
               IntegerToString(shown) + " signal(s) in 24 h", clrSilver);
      ShowLine(3, "Updated " + JsonString(head, "updated_utc") + " | read-only", clrGray);
     }
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
void DrawSignal(const string line)
  {
   string id      = JsonString(line, "id");
   string dir     = JsonString(line, "dir");
   string status  = JsonString(line, "status");
   string swept   = JsonString(line, "pool_label");
   datetime start = (datetime)(long)JsonNumber(line, "t");
   double score   = JsonNumber(line, "score");
   double entry   = JsonNumber(line, "entry");
   double sl      = JsonNumber(line, "sl");
   double tp1     = JsonNumber(line, "tp1");
   double tp2     = JsonNumber(line, "tp2");
   double tp3     = JsonNumber(line, "tp3");
   double pool    = JsonNumber(line, "pool");
   if(id == "" || start <= 0 || entry <= 0 || sl <= 0 || tp1 <= 0)
      return;
   bool buy = (dir == "BUY");
   color main = buy ? InpBuyColor : InpSellColor;
   int bar = PeriodSeconds(_Period);
   datetime finish = start + (datetime)(bar * InpBoxBars);
   double farthest = tp1;
   if(tp2 > 0)
      farthest = tp2;
   if(tp3 > 0)
      farthest = tp3;
   string base = PREFIX + id + "_";

   Box(base + "risk", start, entry, finish, sl, InpRiskFill);
   Box(base + "reward", start, entry, finish, farthest, InpRewardFill);
   Level(base + "entry", start, finish, entry, main, STYLE_SOLID, 2, "Entry " + Price(entry));
   Level(base + "sl", start, finish, sl, InpStopColor, STYLE_SOLID, 1, "SL " + Price(sl));
   Level(base + "tp1", start, finish, tp1, InpTargetColor, STYLE_DASH, 1, "TP1 " + Price(tp1));
   if(tp2 > 0)
      Level(base + "tp2", start, finish, tp2, InpTargetColor, STYLE_DASH, 1, "TP2 " + Price(tp2));
   if(tp3 > 0)
      Level(base + "tp3", start, finish, tp3, InpTargetColor, STYLE_DASH, 1, "TP3 " + Price(tp3));
   if(pool > 0)
      Level(base + "pool", start - (datetime)(bar * 6), start, pool, InpPoolColor, STYLE_DOT, 1,
            "Swept " + swept + " " + Price(pool));

   string text = (buy ? "BUY " : "SELL ") + DoubleToString(score, 0) + " | " + status;
   Text(base + "label", start, sl, text, main, buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER);
  }

//+------------------------------------------------------------------+
string Price(const double value)
  {
   return(DoubleToString(value, _Digits));
  }

//+------------------------------------------------------------------+
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

//+------------------------------------------------------------------+
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
   Text(name + "_text", t2, price, text, clr, ANCHOR_LEFT);
  }

//+------------------------------------------------------------------+
void Text(const string name, const datetime t, const double price, const string text,
          const color clr, const ENUM_ANCHOR_POINT anchor)
  {
   if(!ObjectCreate(0, name, OBJ_TEXT, 0, t, price))
      return;
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
//| One line of the corner box (bottom-left, clear of one-click)     |
//+------------------------------------------------------------------+
void ShowLine(const int row, const string text, const color clr)
  {
   string name = PREFIX + "panel_" + IntegerToString(row);
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 10);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 16 + (3 - row) * 16);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void ShowStatus(const string text, const color clr)
  {
   ObjectsDeleteAll(0, PREFIX);
   ShowLine(3, text, clr);
   ChartRedraw(0);
  }
//+------------------------------------------------------------------+
