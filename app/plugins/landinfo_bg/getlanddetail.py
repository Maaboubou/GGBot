import time
import requests
import json
import urllib3
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_cadastre_data_robust(target_number):
    print(f"--- Searching for Parcel: {target_number} ---")

    chrome_options = Options()
    # chrome_options.add_argument('--headless') # Keep visible for debugging
    chrome_options.add_argument('--start-maximized')  # Maximize window for larger view

    driver = webdriver.Chrome(options=chrome_options)

    # Initialize variables that need to persist after driver closes
    plot_info_text = None

    try:
        # --- PHASE 1: SELENIUM SEARCH & EXTRACTION ---
        print("1. [Selenium] Navigating to map...")
        driver.get("https://kais.cadastre.bg/bg/Map")

        wait = WebDriverWait(driver, 20)

        # Click "Fast Search" tab explicitly
        try:
            tab = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#map-search-tabs li.k-item:first-child")))
            tab.click()
            time.sleep(1)
        except Exception as e:
            print(f"Warning: Could not click tab: {e}")

        # Wait for input
        print("2. [Selenium] Waiting for search input...")
        search_input = wait.until(EC.presence_of_element_located((By.NAME, "KeyWords")))

        # Use JS to interact (bypasses 'not interactable' issues)
        print(f"3. [Selenium] Entering query: {target_number}...")
        driver.execute_script("arguments[0].value = arguments[1];", search_input, target_number)
        driver.execute_script("$(arguments[0]).trigger('change');", search_input)

        # Click search
        print("4. [Selenium] Clicking search...")
        search_btn = driver.find_element(By.ID, "submit-search")
        driver.execute_script("arguments[0].click();", search_btn)

        # Wait for results
        print("5. [Selenium] Waiting for results...")
        time.sleep(8) # Wait for Kendo ajax to finish

        # Extract data item via JS
        print("6. [Selenium] Extracting data item...")
        data_item = driver.execute_script("""
            var listView = $(".resultsList").data("kendoListView");
            if (listView && listView.dataSource.view().length > 0) {
                return listView.dataSource.view()[0];
            }
            return null;
        """)

        if not data_item:
            print("[ERROR] No data found in Selenium search results.")
            return None

        print(f"[SUCCESS] Found Item: {data_item.get('Title')} (Id: {data_item.get('Id')})")

        # --- NEW: SCREENSHOT LOGIC ---
        print("7. [Selenium] Zooming to parcel and taking screenshot...")
        try:
            # Find the zoom button in the first result
            zoom_btn = driver.find_element(By.CSS_SELECTOR, ".resultsList .object .object-option.zoom-js")
            driver.execute_script("arguments[0].click();", zoom_btn)

            # Wait for map to settle/load tiles
            time.sleep(5)

            # Set map scale to 2000 meters FIRST (before switching to satellite)
            print("8. [Selenium] Setting map scale to 2000 meters...")
            try:
                from selenium.webdriver.common.keys import Keys
                from selenium.webdriver.common.action_chains import ActionChains

                # Wait a bit more for the page to fully initialize
                time.sleep(2)

                # Try to find the scale input with multiple selectors
                selectors = [
                    ".mc-mapstat input.k-input-inner[role='spinbutton']",
                    ".mc-mapstat input[role='spinbutton']",
                    "input.k-input-inner[role='spinbutton']",
                    ".k-numerictextbox input"
                ]

                scale_input = None
                for selector in selectors:
                    try:
                        scale_input = driver.find_element(By.CSS_SELECTOR, selector)
                        print(f"[DEBUG] Found input with selector: {selector}")
                        break
                    except:
                        continue

                if scale_input:
                    print("[DEBUG] Using ActionChains to simulate real keyboard input...")

                    # Create ActionChains for realistic interaction
                    actions = ActionChains(driver)

                    # Click on the input to focus it
                    actions.click(scale_input).perform()
                    time.sleep(0.5)

                    # Select all existing text (Ctrl+A)
                    actions.key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).perform()
                    time.sleep(0.3)

                    # Type "2000" character by character
                    actions.send_keys("2000").perform()
                    time.sleep(0.5)

                    # Press Enter
                    actions.send_keys(Keys.RETURN).perform()

                    # Wait for map to adjust to new scale
                    time.sleep(12)
                    print("[SUCCESS] Map scale set via ActionChains keyboard simulation")
                else:
                    print("[WARNING] Could not locate scale input with any selector, skipping scale adjustment")

            except Exception as e:
                print(f"[WARNING] Failed to set map scale: {e}")
                print("[INFO] Continuing without scale adjustment...")

            # Extract plot information from info window
            print("9. [Selenium] Extracting plot information...")
            plot_info_text = None
            try:
                # Click the info icon
                driver.execute_script("""
                    var infoBtn = $(".resultsList .object .object-option.info-js").first();
                    if (infoBtn.length > 0) {
                        infoBtn.click();
                    }
                """)

                # Wait for info window to appear
                time.sleep(3)

                # Extract info window content
                plot_info_text = driver.execute_script("""
                    var content = $("div.k-window-content").first();
                    if (content.length > 0) {
                        return content.text();
                    }
                    return null;
                """)

                if plot_info_text:
                    print(f"[SUCCESS] Extracted plot info ({len(plot_info_text)} characters)")
                else:
                    print("[WARNING] Could not extract plot info from window")

                # Close the info window before taking screenshot
                try:
                    driver.execute_script("""
                        var closeBtn = $("span.k-icon.k-font-icon.k-i-x.k-button-icon").first();
                        if (closeBtn.length > 0) {
                            closeBtn.click();
                        }
                    """)
                    time.sleep(0.5)
                    print("[SUCCESS] Closed info window")
                except Exception as e:
                    print(f"[WARNING] Failed to close info window: {e}")

            except Exception as e:
                print(f"[WARNING] Failed to extract plot info: {e}")

            # Switch to satellite view AFTER scale is set
            print("10. [Selenium] Switching to satellite view...")
            try:
                # Click on Ортофото 2022 satellite layer
                satellite_option = driver.find_element(By.CSS_SELECTOR, "a.baseMap-js[data-layer='orthophoto_2022']")
                driver.execute_script("arguments[0].click();", satellite_option)

                # Wait for tiles to load
                time.sleep(3)
                print("[SUCCESS] Switched to Ортофото 2022 satellite view")
            except Exception as e:
                print(f"[WARNING] Failed to switch to satellite view: {e}")
                # Try to get more info about available layers
                try:
                    layers = driver.find_elements(By.CSS_SELECTOR, "a.baseMap-js")
                    print(f"Available layers: {len(layers)}")
                    for layer in layers:
                        print(f"  - {layer.get_attribute('data-layer')}: {layer.text}")
                except:
                    pass

            # Use parcel number as filename
            screenshot_path = f"{target_number.replace('.', '_')}.png"
            driver.save_screenshot(screenshot_path)
            print(f"[SUCCESS] Screenshot saved to: {screenshot_path}")
        except Exception as e:
            print(f"[WARNING] Failed to take screenshot: {e}")
        # -----------------------------

        # Capture environment for requests
        cookies = driver.get_cookies()
        cookies_dict = {c['name']: c['value'] for c in cookies}
        user_agent = driver.execute_script("return navigator.userAgent;")

        # Try to get CSRF token
        csrf_token = driver.execute_script("return $('[name=\"__RequestVerificationToken\"]').val();")
        if not csrf_token:
            csrf_token = cookies_dict.get('csrf', '')

    except Exception as e:
        print(f"[ERROR] Selenium phase failed: {e}")
        driver.quit()
        return None
    finally:
        # We can close driver now, or keep it if debugging.
        # Closing to save resources.
        driver.quit()

    # --- PHASE 2: REQUESTS GEOMETRY FETCH ---
    print("7. [Requests] Fetching geometry...")
    session = requests.Session()
    session.cookies.update(cookies_dict)

    headers = {
        'User-Agent': user_agent,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-CSRF-TOKEN': csrf_token,
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://kais.cadastre.bg/bg/Map',
        'Origin': 'https://kais.cadastre.bg'
    }

    geom_url = "https://kais.cadastre.bg/bg/Map/GetGeometry/"

    # Construct payload using extracted data
    geom_payload = {
        'IsChecked': 'false',
        'Id': data_item.get('Id'),
        'Type': data_item.get('Type'), # Usually 1 for parcel
        'Number': data_item.get('Number'),
        'Title': data_item.get('Title'),
        'ShortDescription': data_item.get('ShortDescription'),
        'Hash': data_item.get('Hash'),
        '__RequestVerificationToken': csrf_token
    }

    try:
        res = session.post(geom_url, headers=headers, data=geom_payload, timeout=30)

        if res.status_code != 200:
             print(f"[ERROR] Geometry request failed with status: {res.status_code}")
             print(res.text[:200])
             return None

        try:
            geometry_data = res.json()

            # Return combined result
            return {
                'geometry': geometry_data,
                'plot_info': plot_info_text
            }
        except:
             print("[ERROR] Geometry response is not valid JSON")
             print(res.text[:200])
             return None

    except Exception as e:
        print(f"[ERROR] Requests phase failed: {e}")
        return None

if __name__ == "__main__":
    target = "43462.164.31"
    result = get_cadastre_data_robust(target)

    if result:
        print("\n" + "="*60)
        print("地块信息汇总")
        print("="*60)

        # Check if result contains both geometry and plot_info
        if isinstance(result, dict) and 'geometry' in result:
            geometry_data = result['geometry']
            plot_info = result.get('plot_info', '')

            # Extract from geometry (first item in the list)
            if geometry_data and len(geometry_data) > 0:
                attributes = geometry_data[0].get('Attributes', {})

                # 地号
                cadnum = attributes.get('cadnum', 'N/A')
                print(f"\n地号: {cadnum}")

                # 信息
                shortinfo = attributes.get('shortinfo', 'N/A')
                print(f"信息: {shortinfo}")

                # 面积 (取整数)
                area = attributes.get('st_area(shape)', 0)
                area_int = int(area)
                print(f"面积: {area_int} 平方米")

                # 详细信息 (从 plot_info 中提取"Добави в списък с обекти"和"Основна заповед"之间的内容)
                if plot_info:
                    # Find the text between "Добави в списък с обекти" and "Основна заповед"
                    start_marker = "Добави в списък с обекти"
                    end_marker = "Основна заповед"

                    start_idx = plot_info.find(start_marker)
                    end_idx = plot_info.find(end_marker)

                    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                        # Extract the text between markers
                        detailed_text = plot_info[start_idx + len(start_marker):end_idx]
                        # Clean up: strip whitespace and remove excessive newlines
                        detailed_info = ' '.join(detailed_text.split())
                        print(f"详细信息: {detailed_info}")
                    else:
                        print(f"详细信息: (未找到标记)")
                        print(f"  start_marker found: {start_idx != -1}, end_marker found: {end_idx != -1}")
                else:
                    print("详细信息: (未提取到地块信息)")

                print("\n" + "-"*60)
                print("原始数据 (JSON)")
                print("-"*60)
                print("\n=== GEOMETRY DATA ===")
                print(json.dumps(geometry_data, indent=2, ensure_ascii=False))

                if plot_info:
                    print("\n=== PLOT INFORMATION (原始) ===")
                    print(plot_info)
        else:
            # Legacy format (just geometry)
            print(json.dumps(result, indent=2, ensure_ascii=False))

        print("\n" + "="*60)
    else:
        print("\n[FAILED] Could not retrieve data.")