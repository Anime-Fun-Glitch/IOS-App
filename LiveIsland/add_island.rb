# Додає до згенерованого Briefcase-проєкту Xcode: Swift-міст у застосунок + розширення віджета (Live Activity / Dynamic Island).
require 'xcodeproj'
proj_path, app_name = ARGV[0], ARGV[1]
proj = Xcodeproj::Project.open(proj_path)
app = proj.targets.find { |t| t.name == app_name } || proj.targets.find { |t| t.product_type == 'com.apple.product-type.application' }
abort("target not found") unless app
bid = app.build_configurations.first.build_settings['PRODUCT_BUNDLE_IDENTIFIER'] || 'com.animeoffline.animeios'
puts "App target: #{app.name}, bundle id: #{bid}"

group = proj.main_group.new_group('LiveIsland', 'LiveIsland')
shared = group.new_file('AnimeActivity.swift')
bridge = group.new_file('LiveBridge.swift')
widget = group.new_file('AnimeWidget.swift')
group.new_file('Widget-Info.plist')

app.add_file_references([shared, bridge])
app.build_configurations.each do |c|
  c.build_settings['SWIFT_VERSION'] = '5.0'
  c.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '17.0'
  c.build_settings['CLANG_ENABLE_MODULES'] = 'YES'
  c.build_settings['ALWAYS_EMBED_SWIFT_STANDARD_LIBRARIES'] = 'YES'
end

ext = proj.new_target(:app_extension, 'AnimeWidget', :ios, '17.0', nil, :swift)
ext.add_file_references([shared, widget])
ext.build_configurations.each do |c|
  s = c.build_settings
  s['PRODUCT_BUNDLE_IDENTIFIER'] = "#{bid}.widget"
  s['PRODUCT_NAME'] = '$(TARGET_NAME)'
  s['INFOPLIST_FILE'] = 'LiveIsland/Widget-Info.plist'
  s['GENERATE_INFOPLIST_FILE'] = 'NO'
  s['SWIFT_VERSION'] = '5.0'
  s['TARGETED_DEVICE_FAMILY'] = '1,2'
  s['SKIP_INSTALL'] = 'YES'
  s['IPHONEOS_DEPLOYMENT_TARGET'] = '17.0'
  s['LD_RUNPATH_SEARCH_PATHS'] = ['$(inherited)', '@executable_path/Frameworks', '@executable_path/../../Frameworks']
  s['CURRENT_PROJECT_VERSION'] = '1'
  s['MARKETING_VERSION'] = '1.0.0'
  s['SDKROOT'] = 'iphoneos'
end

app.add_dependency(ext)
embed = app.new_copy_files_build_phase('Embed Foundation Extensions')
embed.symbol_dst_subfolder_spec = :plug_ins
bf = embed.add_file_reference(ext.product_reference, true)
bf.settings = { 'ATTRIBUTES' => ['RemoveHeadersOnCopy'] }
proj.save
puts 'OK: Dynamic Island target added'
